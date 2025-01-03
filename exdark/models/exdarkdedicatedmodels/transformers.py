import os
from typing import Optional

import lightning as L
import torch
import wandb
from dotenv import load_dotenv
from lightning.pytorch.callbacks import ModelCheckpoint, LearningRateMonitor
from lightning.pytorch.loggers import WandbLogger
from torch import Tensor
from torchmetrics.detection import MeanAveragePrecision
from transformers import AutoModelForObjectDetection, AutoImageProcessor

from exdark.data.preprocess.labels_storage import exdark_idx2label, exdark_coco_like_labels
from exdark.data.datamodules.exdarkdatamodule import ExDarkDataModule


class DetectionTransformer(L.LightningModule):
    def __init__(
        self,
        transformers_checkpoint: str,
        optimizer: torch.optim.Optimizer,
        scheduler: torch.optim.lr_scheduler._LRScheduler | None,
        num_classes: int = (len(exdark_coco_like_labels)),
        lr_head: float = 0.005,
        lr_backbone: float = 0.0005,
        freeze_backbone: bool = False,
    ):
        super(DetectionTransformer, self).__init__()
        self.model = AutoModelForObjectDetection.from_pretrained(
            transformers_checkpoint,
            num_labels=num_classes,
            ignore_mismatched_sizes=True,
        )
        self.image_processor = AutoImageProcessor.from_pretrained(
            transformers_checkpoint, do_rescale=False
        )
        self.save_hyperparameters()
        self.metric = MeanAveragePrecision()

    @staticmethod
    def pascal_to_coco(pascal_annotations):
        """
        Convert Pascal VOC annotations to COCO format
        Args:
            pascal_annotations: Tuple/List of Pascal VOC annotation dictionaries
        Returns:
            List of COCO format annotations
        """
        coco_annotations = []

        for ann in pascal_annotations:
            # Create new annotation dict for each image
            coco_ann = {"image_id": int(ann["image_id"].item()), "annotations": []}

            # Convert boxes from xyxy to xywh format and create annotations
            boxes = ann["boxes"]
            areas = ann["area"]
            labels = ann["labels"]
            iscrowd = ann["iscrowd"]

            for idx in range(len(boxes)):
                # Convert xyxy to xywh
                x1, y1, x2, y2 = boxes[idx].tolist()
                width = x2 - x1
                height = y2 - y1

                annotation = {
                    "image_id": int(ann["image_id"].item()),
                    "category_id": float(labels[idx].item()),
                    "bbox": [x1, y1, width, height],
                    "iscrowd": int(iscrowd[idx].item()),
                    "area": float(areas[idx].item()),
                }
                coco_ann["annotations"].append(annotation)

            coco_annotations.append(coco_ann)

        return coco_annotations

    def forward(self, images: list[Tensor], targets: Optional[list[dict[str, Tensor]]] = None):
        input_encoding = self.image_processor(
            images=images, annotations=self.pascal_to_coco(targets), return_tensors="pt"
        )
        for key in input_encoding:
            if key == "labels":
                input_encoding[key] = [
                    {k: v.to(self.device) for k, v in t.items()} for t in input_encoding["labels"]
                ]
            else:
                input_encoding[key] = input_encoding[key].to(self.device)
        output_encoding = self.model(**input_encoding)
        return output_encoding

    def training_step(self, batch, batch_idx):
        outputs = self.forward(batch[0], batch[1])
        loss = outputs.loss
        loss_dict = outputs.loss_dict
        self.log("train_loss", loss, prog_bar=True)
        for k, v in loss_dict.items():
            self.log("train_" + k, v.item())
        return loss

    def validation_step(self, batch, batch_idx):
        images, targets = batch
        outputs = self.forward(images, targets)
        loss = outputs.loss
        loss_dict = outputs.loss_dict
        self.log("val_loss", loss, prog_bar=True)
        for k, v in loss_dict.items():
            self.log("val_" + k, v.item())

        preds = self.image_processor.post_process_object_detection(
            outputs,
            target_sizes=torch.tensor([img.shape[1:] for img in images]),
            # threshold=0.4,
        )
        self.metric.update(preds, targets)

    def on_validation_epoch_end(self) -> None:
        mAP = self.metric.compute()
        self.log("val_mAP", mAP["map"], prog_bar=True)
        self.metric.reset()

    def predict_step(self, batch, batch_idx, dataloader_idx=0):
        return self(batch[0])

    def configure_optimizers(self):
        if self.hparams.freeze_backbone:
            for n, p in self.named_parameters():
                if "backbone" in n:
                    p.requires_grad = False
        params = [
            {
                "params": [
                    p
                    for n, p in self.model.named_parameters()
                    if "backbone" in n and p.requires_grad
                ],
                "lr": self.hparams.lr_backbone,
            },
            {
                "params": [p for n, p in self.model.named_parameters() if "backbone" not in n],
                "lr": self.hparams.lr_head,
            },
        ]
        optimizer = self.hparams.optimizer(params=params)
        if self.hparams.scheduler is None:
            return optimizer
        scheduler = self.hparams.scheduler(optimizer=optimizer)
        return [optimizer], [scheduler]


# if __name__ == "__main__":
#     # data
#     batch_size = 6
#     exdark_data = ExDarkDataModule(batch_size)
#
#     # model
#     model = DetectionTransformer(lr=1e-4, lr_backbone=1e-5, weight_decay=1e-4)
#
#     # training specs
#     checkpoints = ModelCheckpoint(
#         save_top_k=1,
#         mode="max",
#         monitor="val_mAP",
#         save_last=True,
#         every_n_epochs=1,
#     )
#
#     # logging
#     load_dotenv()
#     wandb.login(key=os.environ["WANDB_TOKEN"])
#     wandb_logger = WandbLogger(project="exdark")
#     wandb_logger.experiment.config["batch_size"] = batch_size
#     wandb_logger.experiment.config["datamodule"] = "ExDarkDataModule"
#     wandb_logger.experiment.config["modele"] = "SenseTime/deformable-detr-with-box-refine-two-stage"
#     wandb_logger.experiment.config["augmentations"] = "None"
#     lr_monitor = LearningRateMonitor(logging_interval="step", log_momentum=True)
#
#     # training
#     trainer = L.Trainer(
#         accelerator="cpu",
#         max_epochs=100,
#         callbacks=[checkpoints, lr_monitor],
#         # logger=wandb_logger,
#     )
#     trainer.fit(model, datamodule=exdark_data)
