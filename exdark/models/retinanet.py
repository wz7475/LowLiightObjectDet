

import os
from functools import partial
from typing import Optional

import lightning as L
import torch
import torchvision
import wandb
from dotenv import load_dotenv
from lightning.pytorch.callbacks import ModelCheckpoint, LearningRateMonitor
from lightning.pytorch.loggers import WandbLogger
from lightning.pytorch.utilities.types import STEP_OUTPUT
from torch import Tensor
from torch.optim.lr_scheduler import MultiStepLR
from torchmetrics.detection import MeanAveragePrecision
from torchvision.models import ResNet50_Weights, MobileNetV2, MobileNet_V2_Weights
from torchvision.models.detection import FCOS, RetinaNet_ResNet50_FPN_V2_Weights
from torchvision.models.detection.anchor_utils import AnchorGenerator
from torchvision.models.detection.fcos import FCOSClassificationHead
from torchvision.models.detection.retinanet import RetinaNetClassificationHead

from data.labels_storage import exdark_coco_like_labels
from exdark.datamodule import ExDarkDataModule


class Retina(L.LightningModule):
    def __init__(self, num_classes=(len(exdark_coco_like_labels))):
        super(Retina, self).__init__()
        self.weight_decay = 0.005
        self.model = self._get_retina(num_classes)
        self.metric = MeanAveragePrecision()

    @staticmethod
    def _get_retina(num_classes):
        model = torchvision.models.detection.retinanet_resnet50_fpn_v2(
            weights=RetinaNet_ResNet50_FPN_V2_Weights.COCO_V1
        )
        num_anchors = model.head.classification_head.num_anchors
        model.head.classification_head = RetinaNetClassificationHead(
            in_channels=256,
            num_anchors=num_anchors,
            num_classes=num_classes,
            norm_layer=partial(torch.nn.GroupNorm, 32),
        )
        return model

    def forward(self, images: list[Tensor], targets: Optional[list[dict[str, Tensor]]] = None):
        return self.model(images, targets)

    def predict_step(self, batch, batch_idx, dataloader_idx=0):
        return self(batch[0])

    def training_step(self, batch, batch_idx) -> STEP_OUTPUT:
        images, targets = batch
        loss_dict = self.forward(images, targets)
        total_loss = sum(loss for loss in loss_dict.values())
        self.log("train_loss", total_loss)
        return total_loss

    def validation_step(self, batch, batch_idx) -> STEP_OUTPUT:
        images, targets = batch
        preds = self.model(images)
        self.metric.update(preds, targets)

    def on_validation_epoch_end(self) -> None:
        mAP = self.metric.compute()
        self.log("val_mAP", mAP["map"], prog_bar=True)
        self.log("val_mAP_50", mAP["map_50"], prog_bar=True)
        self.log("val_mAP_75", mAP["map_75"], prog_bar=True)
        self.metric.reset()

    def on_fit_start(self) -> None:
        datamodule = self.trainer.datamodule
        self.logger.log_hyperparams(
            {
                "datamodule_name": datamodule.__class__.__name__,
                "batch_size": datamodule.batch_size,
                "dataset_size": len(datamodule.train_dataset),
            }
        )
        print("logged params")

    def configure_optimizers(self):
        lr = 0.01
        params = [
            {
                "params": [
                    p for n, p in model.named_parameters() if "backbone" in n and p.requires_grad
                ],
                "lr": lr / 10,
            },
            {
                "params": [
                    p
                    for n, p in model.named_parameters()
                    if "backbone" not in n and p.requires_grad
                ],
                "lr": lr,
            },
        ]
        optimizer = torch.optim.SGD(params, momentum=0.9, weight_decay=self.weight_decay)
        lr_scheduler = MultiStepLR(optimizer, milestones=[30], gamma=0.1)
        return [optimizer], [lr_scheduler]


if __name__ == "__main__":
    # data
    load_dotenv()
    os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:512"
    batch_size = 16
    exdark_data = ExDarkDataModule(batch_size=batch_size)
    # checkpoints
    checkpoints = ModelCheckpoint(
        save_top_k=1,
        mode="max",
        monitor="val_mAP",
        every_n_epochs=1,
    )
    lr_monitor = LearningRateMonitor(logging_interval="step", log_momentum=True)
    load_dotenv()
    wandb.login(key=os.environ["WANDB_TOKEN"])
    wandb_logger = WandbLogger(project="exdark")
    wandb_logger.experiment.config["batch_size"] = batch_size
    wandb_logger.experiment.config["datamodule"] = "ExDarkDataModule"
    wandb_logger.experiment.config["model"] = "retina"
    wandb_logger.experiment.config["lr_scheduler"] = "none"
    wandb_logger.experiment.config[
        "augmentations"
    ] = """A.RandomGamma(gamma_limit=(80, 120), p=1.0),
            A.Perspective(p=0.1),
            A.HorizontalFlip(p=0.5),
            A.RandomBrightnessContrast(p=0.5),
            A.HueSaturationValue(p=0.1),"""
    # TODO: add datamodule specs logging
    wandb_logger.experiment.config["batch_size"] = batch_size
    model = Retina()
    trainer = L.Trainer(
        accelerator="gpu", max_epochs=150, callbacks=[checkpoints, lr_monitor], logger=wandb_logger
    )
    trainer.fit(model, datamodule=exdark_data)
