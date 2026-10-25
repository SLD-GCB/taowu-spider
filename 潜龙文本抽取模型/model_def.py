import torch
import torch.nn as nn
from common_config import VOCAB_SIZE, EMBED_DIM, CLASS_NUM, DROPOUT_RATE

class EyeModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.embedding = nn.Embedding(VOCAB_SIZE, EMBED_DIM, padding_idx=0)
        self.conv1 = nn.Conv1d(EMBED_DIM, 96, kernel_size=3, padding=1)
        self.ln1 = nn.LayerNorm(96)
        self.conv2 = nn.Conv1d(96, 128, kernel_size=3, padding=1)
        self.ln2 = nn.LayerNorm(128)
        self.conv3 = nn.Conv1d(128, 80, kernel_size=5, padding=2)
        self.ln3 = nn.LayerNorm(80)
        self.conv4 = nn.Conv1d(80, 32, kernel_size=5, padding=2)
        self.ln4 = nn.LayerNorm(32)

        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(DROPOUT_RATE)

        self.gru = nn.GRU(
            input_size=32,
            hidden_size=80,
            num_layers=2,
            bidirectional=True,
            batch_first=True,
            dropout=DROPOUT_RATE
        )
        self.pool = nn.AdaptiveMaxPool1d(1)
        self.fc = nn.Linear(160, CLASS_NUM)

    def forward(self, x):
        embed = self.embedding(x)
        x = embed.transpose(1, 2)

        x = self.relu(self.conv1(x))
        x = self.ln1(x.transpose(1, 2)).transpose(1, 2)
        x = self.dropout(x)

        x = self.relu(self.conv2(x))
        x = self.ln2(x.transpose(1, 2)).transpose(1, 2)
        x = self.dropout(x)

        x = self.relu(self.conv3(x))
        x = self.ln3(x.transpose(1, 2)).transpose(1, 2)
        x = self.dropout(x)

        x = self.relu(self.conv4(x))
        x = self.ln4(x.transpose(1, 2)).transpose(1, 2)
        x = self.dropout(x)

        x = x.transpose(1, 2)
        gru_out, _ = self.gru(x)
        gru_out = gru_out.transpose(1, 2)

        feat = self.pool(gru_out).squeeze(-1)
        return self.fc(feat)