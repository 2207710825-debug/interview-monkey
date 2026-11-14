"""检索、Prompt、LLM 流与会话级取消的核心服务。"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import AsyncIterator, Iterable, Mapping, Sequence

from ..contracts import Channel, Hotword, StreamEvent, Transcript, TranscriptKind
from ..latency import LatencyTracker
from ..rag.context import ConversationContext, PromptContextManager
from ..rag.query import (
    build_query_variants,
    is_generic_technical_question,
    is_product_category,
    reciprocal_rank_fusion,
)
from .providers import LLMProviderError


class RealtimeAnswerService:
    def __init__(
        self,
        retriever,
        llm_provider,
        system_prompt_path: str | Path,
        *,
        top_k: int = 4,
        min_score: float = 0.35,
        token_budget: int = 6000,
        transcript_corrector=None,
        correction_timeout_ms: int = 300,
        hotwords: Iterable[Hotword] = (),
        generic_product_skip_score: float = 0.75,
    ):
        self.retriever = retriever
        self.llm_provider = llm_provider
        self.system_prompt_path = Path(system_prompt_path)
        self.top_k = top_k
        self.min_score = min_score
        self.token_budget = token_budget
        self.transcript_corrector = transcript_corrector
        self.correction_timeout_ms = correction_timeout_ms
        self.hotwords = tuple(hotwords)
        self.generic_product_skip_score = generic_product_skip_score
        self._contexts: dict[str, ConversationContext] = {}
        self._cancels: dict[str, asyncio.Event] = {}

    def _context(self, session_id: str) -> ConversationContext:
        return self._contexts.setdefault(session_id, ConversationContext())

    async def _retrieve_queries(
        self,
        queries: Sequence[str],
        *,
        exclude_categories: Sequence[str] = (),
    ) -> list:
        ranked_lists = await asyncio.gather(*(
            self.retriever.retrieve(
                query,
                top_k=self.top_k,
                min_score=self.min_score,
                exclude_categories=exclude_categories,
            )
            for query in queries
        ))
        return reciprocal_rank_fusion(ranked_lists, limit=self.top_k)

    async def retrieve_context(
        self,
        question: str,
        *,
        raw_question: str | None = None,
        glossary_candidates: Sequence[Mapping[str, object]] = (),
    ) -> tuple[list, dict]:
        queries = build_query_variants(
            question,
            raw_question=raw_question,
            candidates=glossary_candidates,
            hotwords=self.hotwords,
        )
        chunks = await self._retrieve_queries(queries)
        product_filtered = False
        product_categories = tuple(dict.fromkeys(
            str(chunk.metadata.get("category", ""))
            for chunk in chunks
            if is_product_category(str(chunk.metadata.get("category", "")))
        ))
        has_product_chunks = bool(product_categories)
        highest_score = max((chunk.score for chunk in chunks), default=0.0)
        if (
            has_product_chunks
            and highest_score < self.generic_product_skip_score
            and is_generic_technical_question(question, self.hotwords)
        ):
            chunks = await self._retrieve_queries(
                queries,
                exclude_categories=product_categories,
            )
            product_filtered = True
        return chunks, {"queries": queries, "product_filtered": product_filtered}

    def clear_context(self, session_id: str) -> None:
        self._contexts.pop(session_id, None)

    async def cancel(self, session_id: str) -> None:
        previous = self._cancels.get(session_id)
        if previous:
            previous.set()

    async def stream_answer(
        self,
        question: str,
        *,
        session_id: str = "default",
        recent: Sequence[str] = (),
        trace_id: str | None = None,
        tracker: LatencyTracker | None = None,
        suspicious_candidates: Sequence[Mapping[str, object]] = (),
        raw_question: str | None = None,
        asr_alternatives: Sequence[str] = (),
    ) -> AsyncIterator[StreamEvent]:
        question = (question or "").strip()
        if not question:
            raise ValueError("question 不能为空")
        await self.cancel(session_id)
        cancel = asyncio.Event()
        self._cancels[session_id] = cancel
        tracker = tracker or (LatencyTracker(trace_id=trace_id) if trace_id else LatencyTracker())
        tracker.mark("speech_end")
        tracker.mark("asr_final")
        context = self._context(session_id)
        now_ms = int(time.time() * 1000)
        for index, text in enumerate(recent):
            if text.strip():
                context.append(Transcript(
                    TranscriptKind.FINAL, text.strip(), Channel.LOOPBACK,
                    now_ms - (len(recent) - index) * 1000, now_ms, "context",
                ))

        try:
            tracker.mark("rag_start")
            candidates = sorted(
                suspicious_candidates,
                key=lambda item: float(item.get("score", 0.0) or 0.0),
                reverse=True,
            )[:3]
            retrieval_task = asyncio.create_task(
                self.retrieve_context(
                    question,
                    raw_question=raw_question,
                    glossary_candidates=candidates,
                )
            )
            # 两层异步包装都让出一次调度，确保底层检索协程已开始再等待纠错。
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            llm_correction = None
            if self.transcript_corrector and suspicious_candidates:
                try:
                    llm_correction = await asyncio.wait_for(
                        self.transcript_corrector.correct(
                            question,
                            candidates,
                            recent[-3:],
                            raw_text=raw_question,
                            asr_alternatives=asr_alternatives,
                        ),
                        timeout=self.correction_timeout_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    llm_correction = None
                except Exception:
                    llm_correction = None
            chunks, retrieval_meta = await retrieval_task
            tracker.mark("rag_end")
            final_question = str(llm_correction["corrected"]) if llm_correction else question
            prompt = PromptContextManager(self.system_prompt_path, context)
            messages = prompt.build(
                final_question,
                chunks,
                self.token_budget,
                glossary_candidates=candidates,
            )
            context.append(Transcript(
                TranscriptKind.FINAL, final_question, Channel.LOOPBACK, now_ms, now_ms, "question",
                trace_id=tracker.trace_id,
            ))
            yield StreamEvent("meta", tracker.trace_id, {
                "trace_id": tracker.trace_id,
                "question": final_question,
                "raw_question": raw_question or question,
                "llm_correction": llm_correction,
                "sources": [
                    {"chunk_id": chunk.id, "source": chunk.source, "section": chunk.section, "score": chunk.score}
                    for chunk in chunks
                ],
                "retrieval_queries": retrieval_meta["queries"],
                "product_filter_applied": retrieval_meta["product_filtered"],
                "low_confidence": not chunks,
                "rag_ms": tracker.elapsed_ms("rag_start", "rag_end"),
            })

            tracker.mark("llm_request")
            pieces = 0
            answer_parts: list[str] = []
            async for delta in self.llm_provider.stream(messages, cancel):
                if cancel.is_set():
                    report = tracker.finish("interrupted", answer_chars=len("".join(answer_parts)))
                    yield StreamEvent("done", tracker.trace_id, {"status": "interrupted", "metrics": report["metrics"]})
                    return
                if not answer_parts:
                    tracker.mark("llm_first_token")
                answer_parts.append(delta)
                pieces += 1
                yield StreamEvent("token", tracker.trace_id, {"delta": delta})

            status = "interrupted" if cancel.is_set() else "done"
            report = tracker.finish(status, answer_chars=len("".join(answer_parts)))
            yield StreamEvent("done", tracker.trace_id, {
                "status": status,
                "pieces": pieces,
                "metrics": report["metrics"],
            })
        except asyncio.CancelledError:
            pending_retrieval = locals().get("retrieval_task")
            if pending_retrieval and not pending_retrieval.done():
                pending_retrieval.cancel()
                await asyncio.gather(pending_retrieval, return_exceptions=True)
            cancel.set()
            raise
        except LLMProviderError as exc:
            report = tracker.finish("error", category=exc.category)
            yield StreamEvent("error", tracker.trace_id, {
                "category": exc.category,
                "message": str(exc),
                "retryable": exc.retryable,
                "metrics": report["metrics"],
            })
        except Exception as exc:
            report = tracker.finish("error", category="internal")
            yield StreamEvent("error", tracker.trace_id, {
                "category": "internal",
                "message": str(exc),
                "retryable": False,
                "metrics": report["metrics"],
            })
        finally:
            if self._cancels.get(session_id) is cancel:
                self._cancels.pop(session_id, None)
