"""
Document Structural Classifier — DocRex ONNX

Production-grade document classification using vivekkaushal/DocRex
(MobileNetV3-Small, 98.35% accuracy, 6MB, <100ms on CPU).

Classifies: invoice | bank_statement | other
Used as the pre-flight gate before expensive OCR/VLM processing.
"""

import json
import logging
from pathlib import Path
from typing import Optional

import cv2
import fitz  # PyMuPDF
import numpy as np
import onnxruntime as ort
from PIL import Image

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Model paths (downloaded at build time or mounted via volume)
MODEL_DIR = Path(__file__).parent.parent.parent / "models"
MODEL_PATH = MODEL_DIR / "invoice_classifier_fp32.onnx"
# invoice_classifier_fp32.onnx uses external-data serialization: the model
# cannot load without its sibling .data file next to it.
MODEL_DATA_PATH = MODEL_DIR / "invoice_classifier_fp32.onnx.data"
LABELS_PATH = MODEL_DIR / "labels.json"

# ImageNet normalization constants
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(3, 1, 1)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(3, 1, 1)


class DocClassifier:
    """Production document classifier using DocRex ONNX model.

    Loads the model once at initialization and reuses the session
    for all subsequent classifications.
    """

    def __init__(self):
        self.settings = get_settings()
        self.session: Optional[ort.InferenceSession] = None
        self.labels: dict[int, str] = {}
        self._load_model()

    def _load_model(self) -> None:
        """Load the ONNX model and labels at startup."""
        if not MODEL_PATH.exists():
            raise FileNotFoundError(
                f"DocRex ONNX model not found at {MODEL_PATH}. "
                f"Download from: https://huggingface.co/vivekkaushal/DocRex "
                f"and place invoice_classifier_fp32.onnx in {MODEL_DIR}/"
            )

        if not MODEL_DATA_PATH.exists():
            raise FileNotFoundError(
                f"DocRex ONNX external data file not found at {MODEL_DATA_PATH}. "
                f"invoice_classifier_fp32.onnx uses external-data serialization and "
                f"cannot load without its sibling .data file. Download both from "
                f"https://huggingface.co/vivekkaushal/DocRex and place them in {MODEL_DIR}/"
            )

        if not LABELS_PATH.exists():
            raise FileNotFoundError(
                f"Labels file not found at {LABELS_PATH}. "
                f"Download labels.json from https://huggingface.co/vivekkaushal/DocRex"
            )

        self.session = ort.InferenceSession(
            str(MODEL_PATH),
            providers=["CPUExecutionProvider"],
        )

        with open(LABELS_PATH) as f:
            raw_labels = json.load(f)
            # labels.json can be a list ["bank_statement", "invoice", "other"] or dict {"0": ...}
            if isinstance(raw_labels, list):
                self.labels = {i: label for i, label in enumerate(raw_labels)}
            else:
                self.labels = {int(k): v for k, v in raw_labels.items()}

        logger.info(
            "DocRex ONNX model loaded",
            extra={"model_path": str(MODEL_PATH), "labels": self.labels},
        )

    def _preprocess_image(self, image: np.ndarray) -> np.ndarray:
        """Preprocess a BGR OpenCV image for DocRex inference.

        Steps:
        1. Convert BGR -> RGB
        2. Convert to PIL Image
        3. Resize shorter edge to 256
        4. Center crop to 224x224
        5. Scale to [0, 1]
        6. ImageNet normalize
        7. Transpose to NCHW format
        """
        # BGR -> RGB
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)

        # Resize shorter edge to 256
        w, h = pil_img.size
        new_shorter = 256
        if w < h:
            new_w = new_shorter
            new_h = int(h * new_shorter / w)
        else:
            new_h = new_shorter
            new_w = int(w * new_shorter / h)
        pil_img = pil_img.resize((new_w, new_h), Image.BILINEAR)

        # Center crop to 224x224
        left = (new_w - 224) // 2
        top = (new_h - 224) // 2
        pil_img = pil_img.crop((left, top, left + 224, top + 224))

        # To numpy, scale, normalize, transpose
        x = np.asarray(pil_img, dtype=np.float32).transpose(2, 0, 1) / 255.0
        x = ((x - IMAGENET_MEAN) / IMAGENET_STD)[None].astype(np.float32)
        return x

    def _render_pdf_first_page(self, pdf_path: Path) -> np.ndarray:
        """Render the first page of a PDF as a BGR numpy array for classification."""
        doc = fitz.open(str(pdf_path))
        page = doc[0]
        mat = fitz.Matrix(200 / 72, 200 / 72)
        pix = page.get_pixmap(matrix=mat)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
        if pix.n == 4:
            img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
        elif pix.n == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        doc.close()
        return img

    def classify(self, file_path: Path) -> tuple[str, float]:
        """Classify a document and return (label, confidence).

        Args:
            file_path: Path to PDF, JPEG, or PNG file.

        Returns:
            Tuple of (predicted_label, confidence_score).
            Labels: "invoice", "bank_statement", "other"
            Confidence: 0.0 to 1.0

        Raises:
            FileNotFoundError: If model files are missing.
            RuntimeError: If inference fails.
        """
        if self.session is None:
            raise RuntimeError("DocRex model not loaded. Check model files.")

        # Render to numpy array
        suffix = file_path.suffix.lower()
        if suffix == ".pdf":
            image = self._render_pdf_first_page(file_path)
        else:
            image = cv2.imread(str(file_path))
            if image is None:
                raise RuntimeError(f"Cannot read image file: {file_path}")

        # Preprocess
        input_tensor = self._preprocess_image(image)

        # Inference
        try:
            logits = self.session.run(["logits"], {"input": input_tensor})[0][0]
        except Exception as e:
            raise RuntimeError(f"ONNX inference failed: {e}")

        # Softmax
        probs = np.exp(logits - logits.max())
        probs /= probs.sum()

        predicted_idx = int(probs.argmax())
        confidence = float(probs[predicted_idx])
        label = self.labels.get(predicted_idx, "unknown")

        logger.info(
            "Document classified",
            extra={
                "label": label,
                "confidence": confidence,
                "file": str(file_path),
                "all_probs": {self.labels[i]: float(probs[i]) for i in range(len(self.labels))},
            },
        )

        return label, confidence


# Singleton instance (loaded once at app startup)
_classifier: Optional[DocClassifier] = None


def get_classifier() -> DocClassifier:
    """Get or create the singleton DocClassifier instance."""
    global _classifier
    if _classifier is None:
        _classifier = DocClassifier()
    return _classifier
