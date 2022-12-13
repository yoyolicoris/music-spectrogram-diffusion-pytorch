from collections import defaultdict
import pytorch_lightning as pl
import torch
from torch import nn
import torch_optimizer as optim
import torch.nn.functional as F
from torchaudio.transforms import MelSpectrogram

from models.ar_decoder import MIDI2SpecAR
from .mel import MelFeature
from .scaler import get_scaler
from .eval_utils import get_models, calculate_metrics, aggregate_metrics, get_wav, StreamingMultivariateGaussian


class AutoregressiveLM(pl.LightningModule):
    def __init__(self,
                 num_emb: int = 900,
                 output_dim: int = 128,
                 max_input_length: int = 2048,
                 max_output_length: int = 512,
                 emb_dim: int = 512,
                 dim_feedforward: int = 1024,
                 nhead: int = 6,
                 head_dim: int = 64,
                 num_layers: int = 8,
                 dropout: float = 0.1,
                 layer_norm_eps: float = 1e-5,
                 norm_first: bool = True,
                 **mel_kwargs) -> None:
        super().__init__()

        self.model = MIDI2SpecAR(
            num_emb=num_emb, output_dim=output_dim, max_input_length=max_input_length,
            max_output_length=max_output_length, emb_dim=emb_dim, nhead=nhead,
            head_dim=head_dim, num_encoder_layers=num_layers, num_decoder_layers=num_layers, dropout=dropout,
            layer_norm_eps=layer_norm_eps, norm_first=norm_first, dim_feedforward=dim_feedforward
        )

        self.mel = nn.Sequential(
            MelFeature(window_fn=torch.hann_window, **mel_kwargs),
            get_scaler()
        )

    def forward(self, midi, *args, rescale=True, **kwargs):
        y = self.model.infer(midi, *args, **kwargs)
        if rescale:
            y = self.mel[1].reverse(y)
        return y

    def training_step(self, batch, batch_idx):
        midi, wav, *_ = batch
        spec = self.mel(wav)
        past_spec = spec.roll(1, dims=1)
        past_spec[:, 0] = 0
        pred = self.model(midi, past_spec)
        loss = F.mse_loss(pred, spec)

        values = {
            'loss': loss,
        }
        self.log_dict(values, prog_bar=False, sync_dist=True)
        return loss

    def validation_step(self, batch, batch_idx):
        midi, wav, *_ = batch
        spec = self.mel(wav)
        past_spec = spec.roll(1, dims=1)
        past_spec[:, 0] = 0
        pred = self.model(midi, past_spec)
        loss = F.mse_loss(pred, spec)
        return loss.item()

    def validation_epoch_end(self, outputs) -> None:
        avg_loss = sum(outputs) / len(outputs)
        self.log('val_loss', avg_loss, prog_bar=True, sync_dist=True)

    def configure_optimizers(self):
        return optim.Adafactor(self.parameters(), lr=1e-3)

    def on_test_start(self) -> None:
        vggish_model, trill_model, self.melgan = get_models()
        self.vggish_fn = lambda x, sr: vggish_model(x)
        self.trill_fn = lambda x, sr: trill_model(
            x, sample_rate=sr)['embedding']

        self.true_dists = defaultdict(StreamingMultivariateGaussian)
        self.pred_dists = defaultdict(StreamingMultivariateGaussian)

        return super().on_test_start()

    def test_step(self, batch, batch_idx):
        midi, orig_wav, *_ = batch
        spec = self.mel(orig_wav)
        pred = self.model.infer(midi)
        loss = F.mse_loss(pred, spec)
        pred_wav = self.spec_to_wav(pred)
        orig_wav = orig_wav.cpu().numpy()
        metric = calculate_metrics(
            orig_wav, pred_wav, self.vggish_fn, self.trill_fn, self.true_dists, self.pred_dists)
        metric["loss"] = loss
        return metric

    def test_epoch_end(self, outputs) -> None:
        metrics = aggregate_metrics(outputs, self.true_dists, self.pred_dists)
        self.log_dict(metrics, prog_bar=True, sync_dist=True)

        return super().test_epoch_end(outputs)

    def spec_to_wav(self, spec):
        return get_wav(self.melgan, spec)
