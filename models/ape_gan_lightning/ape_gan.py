import pytorch_lightning as pl
import torch
from torch import nn

from .models import MnistCNN, CifarCNN, Generator, Discriminator
from ..adv_gan_lightning.target_model import TargetModel

from torchmetrics.functional import accuracy
import wandb

class ApeGan(pl.LightningModule):
    def __init__(
            self, 
            in_ch=1, 
            gen_loss_scale=0.7, 
            dis_loss_scale=0.3, 
            lr=2e-4, 
            attack=None,
            target_model_dir=None,
            num_batches_to_log = 1,
            num_samples_to_log = 16,
        ):
        super().__init__()
        
        self.gen_loss_scale = gen_loss_scale
        self.dis_loss_scale = dis_loss_scale
        self.lr = lr
        
        self.generator = Generator(in_ch)
        self.discriminator = Discriminator(in_ch)
    
        self.attack = attack

        self.loss_bce = nn.BCEWithLogitsLoss()
        self.loss_mse = nn.MSELoss()

        self.num_batches_to_log = num_batches_to_log
        self.num_samples_to_log = num_samples_to_log

        if target_model_dir is not None:
            self.target_model = TargetModel.load_from_checkpoint(checkpoint_path=target_model_dir)
            self.target_model.freeze()
            self.target_model.eval()

        self.attack_batches = []

    def forward(self, z):
        return self.generator(z)

    def training_step(self, batch, batch_idx, optimizer_idx):
        X, X_adv = batch

        if self.attack is not None:
            y = X_adv.clone()
            X_adv = self.attack(X)

        t_real = torch.ones(X.shape[0], device=self.device)
        t_fake = torch.zeros(X.shape[0], device=self.device)

        if optimizer_idx == 0:
            X_fake = self.generator(X_adv)
            y_fake = self.discriminator(X_fake)

            loss_generator = self.gen_loss_scale * self.loss_mse(X_fake, X) + self.dis_loss_scale * self.loss_bce(y_fake, t_real)

            losses = {
                "train_loss_generator": loss_generator
            }

            self.log_dict(
                losses,
                prog_bar=True,
                on_step=True,
                on_epoch=True
            )

            return loss_generator
        elif optimizer_idx == 1:
            y_real = self.discriminator(X)
            X_fake = self.generator(X_adv)
            y_fake = self.discriminator(X_fake)

            loss_discriminator = self.loss_bce(y_real, t_real) + self.loss_bce(y_fake, t_fake)

            losses = {
                "train_loss_discriminiator": loss_discriminator,
            }

            self.log_dict(
                losses,
                prog_bar=True,
                on_step=True,
                on_epoch=True
            )

            return loss_discriminator

    def validation_step(self, batch, batch_idx):
        X, X_adv = batch            

        if self.attack is not None:
            y = X_adv.clone()
            
            """
            if self.current_epoch == 0:
                X_adv = self.attack(X)
                self.attack_batches.append(X_adv)
            else:
                X_adv = self.attack_batches[batch_idx]
            """
            
            X_adv = self.attack(X)
            X_res = self.generator(X_adv)

            y_original_pred, y_adversarial_pred, y_restored_pred = self.target_model_metrics(X, y, X_adv, X_res)

        t_real = torch.ones(X.shape[0], device=self.device)
        t_fake = torch.zeros(X.shape[0], device=self.device)

        y_real = self.discriminator(X)
        X_fake = self.generator(X_adv)
        y_fake = self.discriminator(X_fake)

        loss_discriminator = self.loss_bce(y_real, t_real) + self.loss_bce(y_fake, t_fake)
        loss_generator = self.gen_loss_scale * self.loss_mse(X_fake, X) + self.dis_loss_scale * self.loss_bce(y_fake, t_real)

        losses = {
            "validation_loss_discriminiator": loss_discriminator,
            "validation_loss_generator": loss_generator
        }

        self.log_dict(
            losses,
            prog_bar=True,
            on_step=True,
            on_epoch=True
        )

        return X, y, X_adv, X_fake, y_original_pred, y_adversarial_pred, y_restored_pred

    def target_model_metrics(self, imgs, labels, adv_imgs, res_imgs, stage='validation'):
        y_original_pred = self.target_model(imgs).argmax(1)
        y_adversarial_pred = self.target_model(adv_imgs).argmax(1)
        y_restored_pred = self.target_model(res_imgs).argmax(1)

        accuracy_original = accuracy(y_original_pred, labels)
        accuracy_adversarial = accuracy(y_adversarial_pred, labels)
        accuracy_restored = accuracy(y_restored_pred, labels)

        losses = {
            f"{stage}_accuracy_original": accuracy_original,
            f"{stage}_accuracy_adversarial": accuracy_adversarial,
            f"{stage}_accuracy_restored": accuracy_restored,
        }

        self.log_dict(
            losses,
            prog_bar=True,
            on_step=True,
            on_epoch=True
        )

        return y_original_pred, y_adversarial_pred, y_restored_pred

    def validation_epoch_end(self, outputs):
        imgs_batches, labels_batches, adv_imgs_batches, res_imgs_batches, y_original_pred, y_adversarial_pred, y_restored_pred = [torch.stack([output[i] for output in outputs])[:self.num_batches_to_log, :self.num_samples_to_log] for i in range(len(outputs[0]))]

        wandb.log({
            "original_imgs": [
                wandb.Image(
                    img,
                    caption=f'Pred: {pred}, Label: {label}'
                ) for imgs, labels, preds in zip(imgs_batches, labels_batches, y_original_pred) for img, pred, label in zip(imgs, labels, preds)
            ] if self.current_epoch == 0 else None,
            "attack_imgs": [
                wandb.Image(
                    adv_img,
                    caption=f'Pred: {pred}, Label: {label}'
                ) for adv_imgs, labels, preds in zip(adv_imgs_batches, labels_batches, y_adversarial_pred) for adv_img, pred, label in zip(adv_imgs, labels, preds)
            ] if self.current_epoch == 0 else None,
            "restored_imgs": [
                wandb.Image(
                    res_img,
                    caption=f'Pred: {pred}, Label: {label}'
                ) for res_imgs, labels, preds in zip(res_imgs_batches, labels_batches, y_restored_pred) for res_img, pred, label in zip(res_imgs, labels, preds)
            ],
        })

    def optimizer_step(
        self,
        epoch,
        batch_idx,
        optimizer,
        optimizer_idx,
        optimizer_closure,
        on_tpu=False,
        using_native_amp=False,
        using_lbfgs=False,
    ):
        # update generator twice
        if optimizer_idx == 0:
            optimizer.step(closure=optimizer_closure)
            optimizer.step(closure=optimizer_closure)

        if optimizer_idx == 1:
            optimizer.step(closure=optimizer_closure)

    def configure_optimizers(self):        
        opt_d = torch.optim.Adam(self.discriminator.parameters(), lr=self.lr, betas=(0.5, 0.999))
        opt_g = torch.optim.Adam(self.generator.parameters(), lr=self.lr, betas=(0.5, 0.999))
        
        return [opt_g, opt_d], []