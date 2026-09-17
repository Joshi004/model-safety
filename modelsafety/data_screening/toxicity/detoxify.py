"""Adapted from UnitaryAI Detoxify; modified for this repository.

Upstream: https://github.com/unitaryai/detoxify
License: Apache-2.0. See THIRD_PARTY_NOTICES.md and LICENSES/Apache-2.0.txt.
"""

import torch
import transformers
from tqdm import tqdm

DOWNLOAD_URL = "https://github.com/unitaryai/detoxify/releases/download/"
MODEL_URLS = {
    "original": DOWNLOAD_URL + "v0.1-alpha/toxic_original-c1212f89.ckpt",
    "unbiased": DOWNLOAD_URL + "v0.3-alpha/toxic_debiased-c7548aa0.ckpt",
    "multilingual": DOWNLOAD_URL + "v0.4-alpha/multilingual_debiased-0b549669.ckpt",
    "original-small": DOWNLOAD_URL + "v0.1.2/original-albert-0e1d6498.ckpt",
    "unbiased-small": DOWNLOAD_URL + "v0.1.2/unbiased-albert-c8519128.ckpt",
}

PRETRAINED_MODEL = None


def get_model_and_tokenizer(
    model_type, model_name, tokenizer_name, num_classes, state_dict, huggingface_config_path=None
):
    model_class = getattr(transformers, model_name)
    config = model_class.config_class.from_pretrained(model_type, num_labels=num_classes)
    model = model_class.from_pretrained(
        pretrained_model_name_or_path=None,
        config=huggingface_config_path or config,
        state_dict=state_dict,
        local_files_only=huggingface_config_path is not None,
    )
    tokenizer = getattr(transformers, tokenizer_name).from_pretrained(
        huggingface_config_path or model_type,
        local_files_only=huggingface_config_path is not None,
        # TODO: may be needed to let it work with Kaggle competition
        # model_max_length=512,
    )

    return model, tokenizer


def load_checkpoint(model_type="original", checkpoint=None, device="cpu", huggingface_config_path=None):
    if checkpoint is None:
        checkpoint_path = MODEL_URLS[model_type]
        loaded = torch.hub.load_state_dict_from_url(checkpoint_path, map_location=device)
    else:
        loaded = torch.load(checkpoint, map_location=device)
        if "config" not in loaded or "state_dict" not in loaded:
            raise ValueError(
                "Checkpoint needs to contain the config it was trained \
                    with as well as the state dict"
            )
    class_names = loaded["config"]["dataset"]["args"]["classes"]
    # standardise class names between models
    change_names = {
        "toxic": "toxicity",
        "identity_hate": "identity_attack",
        "severe_toxic": "severe_toxicity",
    }
    class_names = [change_names.get(cl, cl) for cl in class_names]
    model, tokenizer = get_model_and_tokenizer(
        **loaded["config"]["arch"]["args"],
        state_dict=loaded["state_dict"],
        huggingface_config_path=huggingface_config_path,
    )

    return model, tokenizer, class_names


def load_model(model_type, checkpoint=None):
    if checkpoint is None:
        model, _, _ = load_checkpoint(model_type=model_type)
    else:
        model, _, _ = load_checkpoint(checkpoint=checkpoint)
    return model


class Detoxify:
    """Detoxify
    Easily predict if a comment or list of comments is toxic.
    Can initialize 5 different model types from model type or checkpoint path:
        - original:
            model trained on data from the Jigsaw Toxic Comment
            Classification Challenge
        - unbiased:
            model trained on data from the Jigsaw Unintended Bias in
            Toxicity Classification Challenge
        - multilingual:
            model trained on data from the Jigsaw Multilingual
            Toxic Comment Classification Challenge
        - original-small:
            lightweight version of the original model
        - unbiased-small:
            lightweight version of the unbiased model
    Args:
        model_type(str): model type to be loaded, can be either original,
                         unbiased or multilingual
        checkpoint(str): checkpoint path, defaults to None
        device(str or torch.device): accepts any torch.device input or
                                     torch.device object, defaults to cpu
        huggingface_config_path: path to HF config and tokenizer files needed for offline model loading
    Returns:
        results(dict): dictionary of output scores for each class
    """

    def __init__(self, model_type="original", checkpoint=PRETRAINED_MODEL, device="cpu", huggingface_config_path=None):
        super().__init__()
        self.model, self.tokenizer, self.class_names = load_checkpoint(
            model_type=model_type,
            checkpoint=checkpoint,
            device=device,
            huggingface_config_path=huggingface_config_path,
        )
        self.device = device
        self.model.to(self.device)

    @torch.no_grad()
    def predict(self, text, batch_size=32):
        self.model.eval()
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")

        texts = [text] if isinstance(text, str) else list(text)
        chunks = []
        owners = []
        chunk_size = self.tokenizer.model_max_length

        # Flatten chunks from every message so each GPU forward pass can contain
        # chunks belonging to different messages. The previous implementation
        # completed one message before starting the next, which left most
        # batches nearly empty.
        for owner, item in enumerate(texts):
            input_ids = self.tokenizer.encode(item)
            for start in range(0, len(input_ids), chunk_size):
                chunks.append(input_ids[start : start + chunk_size])
                owners.append(owner)

        best_scores = [None] * len(texts)
        pad_token_id = self.tokenizer.pad_token_id
        if chunks and pad_token_id is None:
            raise ValueError("Tokenizer does not define pad_token_id")

        for start in range(0, len(chunks), batch_size):
            batch_chunks = chunks[start : start + batch_size]
            batch_owners = owners[start : start + batch_size]
            max_length = max(len(ids) for ids in batch_chunks)
            padded_input_ids = []
            attention_masks = []

            for ids in batch_chunks:
                pad_length = max_length - len(ids)
                padded_input_ids.append(ids + [pad_token_id] * pad_length)
                attention_masks.append([1] * len(ids) + [0] * pad_length)

            logits = self.model(
                input_ids=torch.tensor(padded_input_ids, device=self.device),
                attention_mask=torch.tensor(attention_masks, device=self.device),
            )[0]
            scores = torch.sigmoid(logits).cpu()
            for owner, score in zip(batch_owners, scores):
                previous = best_scores[owner]
                best_scores[owner] = (
                    score if previous is None else torch.maximum(previous, score)
                )

        if best_scores:
            if any(score is None for score in best_scores):
                raise RuntimeError("Tokenizer produced no chunks for one or more inputs")
            final_scores = torch.stack(best_scores)
        else:
            final_scores = torch.empty(0, len(self.class_names))

        results = {}
        for i, cla in enumerate(self.class_names):
            results[cla] = final_scores[:, i].squeeze().tolist()
        return results
