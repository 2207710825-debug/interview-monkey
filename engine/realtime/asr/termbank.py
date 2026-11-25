"""从个人笔记/知识库抽取英文技术术语，生成 ASR 热词。

目标不是把笔记里的所有英文都塞进解码器，而是：
- 剥掉代码块、URL、Markdown 装饰和通用英文词；
- 把 YOLO/NMS/Backbone/FPN/PAN/CNN/Conv 这类技术词挑出来；
- 与人工 glossary 合并，人工词条优先；
- 控制解码热词数量，避免拖慢 sherpa-onnx。
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable

from ..contracts import Chunk, Hotword
from .hotwords import is_decoder_term


_FENCED_CODE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`]*`")
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_LATIN = re.compile(r"\b[A-Za-z][A-Za-z0-9+_.-]{1,30}\b")
_LEVEL_TAG = re.compile(r"^[a-z]{1,2}\d+$", re.I)

# 人工维护的规范写法。命中这些 key 时，优先使用这里的英文大小写。
CANONICAL_TERMS: dict[str, str] = {
    "yolo": "YOLO", "yolov5": "YOLOv5", "yolov6": "YOLOv6", "yolov7": "YOLOv7",
    "yolov8": "YOLOv8", "yolov9": "YOLOv9", "yolov10": "YOLOv10",
    "yolo11": "YOLO11", "yolox": "YOLOX",
    "nms": "NMS", "soft-nms": "Soft-NMS", "softnms": "Soft-NMS",
    "iou": "IoU", "giou": "GIoU", "diou": "DIoU", "ciou": "CIoU",
    "unet": "UNet", "u-net": "UNet", "dice": "Dice",
    "mobilenet": "MobileNet", "shufflenet": "ShuffleNet",
    "map": "mAP", "ap": "AP", "ar": "AR",
    "fpn": "FPN", "pan": "PAN", "bifpn": "BiFPN", "neck": "Neck", "head": "Head",
    "cnn": "CNN", "conv": "Conv", "convolution": "Convolution", "backbone": "Backbone",
    "bce": "BCE", "ce": "CE", "focal": "Focal", "focalloss": "Focal Loss",
    "resnet": "ResNet", "resnet50": "ResNet50", "resnet101": "ResNet101",
    "transformer": "Transformer", "vit": "ViT", "swin": "Swin",
    "swintransformer": "Swin Transformer",
    "batchnorm": "BatchNorm", "bn": "BN", "dropout": "Dropout",
    "relu": "ReLU", "sigmoid": "Sigmoid", "softmax": "Softmax",
    "anchor": "Anchor", "anchors": "Anchors", "rpn": "RPN",
    "roi": "RoI", "roialign": "RoI Align", "roipooling": "RoIPooling",
    "sppf": "SPPF", "spp": "SPP", "cspdarknet": "CSPDarknet", "darknet": "DarkNet",
    "onnx": "ONNX", "pytorch": "PyTorch", "tensorflow": "TensorFlow",
    "opencv": "OpenCV", "numpy": "NumPy", "pcl": "PCL", "tensorrt": "TensorRT",
    "cuda": "CUDA", "gpu": "GPU", "cpu": "CPU",
    "pointnet": "PointNet", "nerf": "NeRF", "3dgs": "3DGS", "slam": "SLAM",
    "tsdf": "TSDF", "mvs": "MVS", "sdf": "SDF",
    "pnp": "PnP", "epnp": "EPnP", "p3p": "P3P", "ransac": "RANSAC",
    "icp": "ICP", "gicp": "GICP", "ndt": "NDT", "fpfh": "FPFH",
    "mse": "MSE", "mae": "MAE", "rmse": "RMSE",
    "precision": "Precision", "recall": "Recall",
    "fp16": "FP16", "fp32": "FP32", "int8": "INT8",
    "bilstm": "BiLSTM", "lstm": "LSTM", "gru": "GRU", "rnn": "RNN", "mlp": "MLP",
    "gan": "GAN", "vae": "VAE", "knn": "KNN", "svm": "SVM", "pca": "PCA",
    "tsne": "t-SNE", "auc": "AUC", "roc": "ROC",
    "python": "Python", "c++": "C++", "java": "Java", "docker": "Docker",
    "linux": "Linux", "windows": "Windows", "git": "Git",
}

# 代码、图表、Markdown 结构产生的噪声词。
_NOISE_WORDS = {
    "text", "md", "cpp", "py", "json", "yaml", "html", "css", "url", "http",
    "https", "import", "from", "def", "class", "return", "if", "else", "elif",
    "for", "while", "try", "except", "finally", "with", "as", "in", "is", "not",
    "and", "or", "print", "printf", "include", "define", "int", "float",
    "double", "char", "void", "bool", "std", "cout", "cin", "endl", "main",
    "public", "private", "static", "const", "let", "var", "function", "async",
    "await", "yield", "lambda", "pass", "break", "continue", "frac", "mermaid",
    "flowchart", "partial", "shape", "stevenx", "qq", "hat", "mm",
    "pythondemo", "n0", "n1", "n2", "n3", "n4", "n5", "n6", "n7", "n8", "n9",
}

# 通用英文/技术泛词：它们出现频率很高，但不适合做解码热词。
_GENERIC_WORDS = {
    "the", "and", "of", "in", "on", "to", "for", "with", "a", "an", "is",
    "are", "was", "were", "be", "been", "this", "that", "it", "its", "as",
    "at", "by", "or", "not", "no", "yes", "you", "your", "we", "our", "they",
    "their", "he", "she", "his", "her", "can", "could", "should", "would",
    "will", "may", "might", "do", "does", "did", "have", "has", "had", "more",
    "most", "less", "least", "very", "just", "about", "into", "over", "under",
    "than", "then", "there", "here", "what", "which", "who", "whom", "whose",
    "why", "how", "when", "where", "all", "any", "both", "each", "few",
    "other", "some", "such", "only", "own", "same", "so", "too", "also",
    "model", "data", "train", "test", "value", "result", "method", "function",
    "system", "problem", "process", "code", "file", "line", "type", "name",
    "size", "output", "input", "use", "using", "used", "set", "get", "new",
    "old", "high", "low", "good", "better", "best", "large", "small", "first",
    "last", "next", "previous", "true", "false", "none", "null", "self",
    "image", "feature", "features", "layer", "layers", "step", "steps",
}


@dataclass(frozen=True, slots=True)
class TermBank:
    hotwords: tuple[Hotword, ...]
    decoder_count: int
    unique_terms: int
    manual_count: int


def _strip_noise(text: str) -> str:
    value = _FENCED_CODE.sub(" ", text or "")
    value = _INLINE_CODE.sub(" ", value)
    value = _URL.sub(" ", value)
    return value


def _clean_surface(term: str) -> str:
    return term.strip("._-")


def _count_terms(chunks: Iterable[Chunk]) -> tuple[Counter[str], dict[str, Counter[str]]]:
    counts: Counter[str] = Counter()
    surfaces: dict[str, Counter[str]] = defaultdict(Counter)
    for chunk in chunks:
        section = _strip_noise(chunk.section or "")
        body = _strip_noise(chunk.text or "")
        # 章节标题里的术语通常更“主题化”，给 3 倍权重。
        for value, weight in ((section, 3), (body, 1)):
            for raw in _LATIN.findall(value):
                term = _clean_surface(raw)
                key = term.casefold()
                if len(key) < 2 or len(key) > 32:
                    continue
                if key in _NOISE_WORDS or key in _GENERIC_WORDS:
                    continue
                if key in CANONICAL_TERMS:
                    # 规范词即使只出现一次也值得保留。
                    pass
                elif _LEVEL_TAG.fullmatch(key):
                    continue
                counts[key] += weight
                surfaces[key][term] += 1
    return counts, surfaces


def _weight(count: int, canonical: bool) -> int:
    base = 7 if canonical else 6
    return max(1, min(10, base + int(math.log2(count + 1))))


def merge_hotwords(
    manual: Iterable[Hotword],
    extracted: Iterable[Hotword],
) -> tuple[Hotword, ...]:
    """人工 glossary 优先；自动抽取只补充新 key，但可提升同词权重。

    例如人工词库里的 CNN 权重是 5，而笔记中 CNN 出现频率很高；
    自动抽取给出更高权重时，保留人工的 aliases/category，只升级 weight，
    这样它才能进入 sherpa 解码热词（materialize 只收 weight >= 6）。
    """
    result: list[Hotword] = []
    index: dict[str, int] = {}
    for word in manual:
        key = word.term.casefold()
        if key in index:
            continue
        index[key] = len(result)
        result.append(word)
    for word in extracted:
        key = word.term.casefold()
        if key in index:
            existing = result[index[key]]
            if word.weight > existing.weight:
                result[index[key]] = Hotword(
                    term=existing.term,
                    weight=word.weight,
                    aliases=existing.aliases,
                    category=existing.category,
                )
            continue
        index[key] = len(result)
        result.append(word)
    return tuple(result)


def build_termbank(
    chunks: Iterable[Chunk],
    manual_hotwords: Iterable[Hotword] = (),
    *,
    limit: int = 140,
) -> TermBank:
    """从知识库片段抽取术语，返回可合并的人工 + 自动热词。

    人工词条在前；自动词条最多 ``limit`` 个。解码器最终只读取权重 >=6
    且格式兼容的词，因此实际解码词数由 ``decoder_count`` 给出。
    """
    original_manual = tuple(manual_hotwords)
    counts, surfaces = _count_terms(chunks)

    # 人工词库权重偏低（例如 CNN=5）时，笔记里的高频英文术语可以升级到
    # 解码热词门槛，但保留原来的人工 aliases/category。
    manual: list[Hotword] = []
    for word in original_manual:
        key = word.term.casefold()
        count = counts.get(key, 0)
        if count and re.search(r"[A-Za-z]", word.term) and key in CANONICAL_TERMS:
            weight = max(
                word.weight,
                min(10, 6 + int(math.log2(count + 1))),
            )
            if weight > word.weight:
                word = Hotword(
                    term=word.term,
                    weight=weight,
                    aliases=word.aliases,
                    category=word.category,
                )
        manual.append(word)
    manual = tuple(manual)
    manual_keys = {word.term.casefold() for word in manual}

    entries: list[tuple[int, int, str, str]] = []
    # 先排规范词，再按出现频次；规范词优先保证 YOLO/NMS/FPN 等一定进词表。
    for key, count in counts.items():
        if key in manual_keys:
            continue
        canonical = CANONICAL_TERMS.get(key)
        surface = canonical or surfaces[key].most_common(1)[0][0]
        entries.append((0 if canonical else 1, -count, surface, key))
    entries.sort()

    extracted: list[Hotword] = []
    seen = set(manual_keys)
    for _known, _neg_count, surface, key in entries:
        if key in seen:
            continue
        seen.add(key)
        extracted.append(Hotword(
            term=surface,
            weight=_weight(counts[key], key in CANONICAL_TERMS),
            aliases=(),
            category="knowledge",
        ))
        if len(extracted) >= max(0, limit):
            break

    merged = merge_hotwords(manual, extracted)
    decoder_count = sum(
        1 for word in merged
        if word.weight >= 6 and is_decoder_term(word.term)
    )
    return TermBank(
        hotwords=merged,
        decoder_count=decoder_count,
        unique_terms=len(counts) + len(manual),
        manual_count=len(manual),
    )
