import albumentations as A
import lightning as L
from albumentations.pytorch import ToTensorV2
from lightning.pytorch.utilities.types import EVAL_DATALOADERS, TRAIN_DATALOADERS
from torch.utils.data import DataLoader

from exdark.config import RESIZE_TO, TRAIN_DIR, VALID_DIR, TEST_DIR
from exdark.datasets import ExDarkDataset


class PredictionError(Exception):
    def __init__(self, message="You have to call set_up_predict_data_first"):
        self.message = message
        super().__init__(self.message)


class ExDarkDataModule(L.LightningDataModule):
    def __init__(self, batch_size: int):
        super().__init__()
        self.batch_size = batch_size
        self.train_transforms = A.Compose(
            [
                A.RandomCrop(width=450, height=450),
                A.HorizontalFlip(p=0.5),
                A.RandomBrightnessContrast(p=0.2),
            ],
            bbox_params={
                'format': 'pascal_voc',
                'label_fields': ['labels']
            }
        )
        self.eval_transforms = A.Compose(
            [ToTensorV2(p=1.0)],
            bbox_params={
                'format': 'pascal_voc',
                'label_fields': ['labels']
            })
        self.predict_transforms = A.Compose([ToTensorV2(p=1.0)])

    def setup(self, stage: str) -> None:
        if stage == "fit":
            self.train_dataset = ExDarkDataset(
                TRAIN_DIR, RESIZE_TO, RESIZE_TO, self.train_transforms
            )
            self.val_dataset = ExDarkDataset(
                VALID_DIR, RESIZE_TO, RESIZE_TO, self.eval_transforms
            )
        if stage == "test":
            self.test_dataset = ExDarkDataset(
                TEST_DIR, RESIZE_TO, RESIZE_TO, self.eval_transforms
            )

    def setup_predict_data(self, img_dir: str):
        self.predict_datset = ExDarkDataset(img_dir, RESIZE_TO, RESIZE_TO, transforms=self.predict_transforms)

    @staticmethod
    def _collate_fn(batch):
        """Define a collate function to handle batches."""
        return tuple(zip(*batch))

    def train_dataloader(self) -> TRAIN_DATALOADERS:
        return DataLoader(self.train_dataset, batch_size=self.batch_size, collate_fn=self._collate_fn)

    def test_dataloader(self) -> EVAL_DATALOADERS:
        return DataLoader(self.test_dataset, batch_size=self.batch_size, collate_fn=self._collate_fn)

    def val_dataloader(self) -> EVAL_DATALOADERS:
        return DataLoader(self.val_dataset, batch_size=self.batch_size, collate_fn=self._collate_fn)

    def predict_dataloader(self):
        try:
            return DataLoader(self.predict_datset, batch_size=self.batch_size, collate_fn=self._collate_fn)
        except AttributeError:
            raise PredictionError()