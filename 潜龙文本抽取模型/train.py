import os
import json
import random
import re
import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import StepLR
from sklearn.model_selection import train_test_split
from torch.utils.data import Dataset, DataLoader

# ===================== 全局配置 =====================
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
VOCAB_SIZE = 8000
EMBED_DIM = 80
CLASS_NUM = 8
BATCH_SIZE = 4
LR = 7e-6
WEIGHT_DECAY = 2e-4
DROPOUT_RATE = 0.4
MAX_EPOCH = 2000
MAX_SEQ_LEN = 600
PATIENCE = 10  # 连续10轮不提升则早停

# 路径
BASE_DIR = os.path.join("model_out", "eye_plugin")
os.makedirs(BASE_DIR, exist_ok=True)
DATA_PATH = os.path.join("dataset", "job_dataset_with_pos.json")
VOCAB_SAVE_PATH = os.path.join(BASE_DIR, "dynamic_vocab.json")
MODEL_WEIGHT_PATH = os.path.join(BASE_DIR, "eye_model.pth")

# ===================== FocalLoss 适配多标签 =====================
class FocalLoss(nn.Module):
    def __init__(self, alpha=1.0, gamma=2.0):
        super().__init__()
        self.alpha = alpha
        self.gamma = gamma

    def forward(self, logits, targets):
        prob = torch.sigmoid(logits)
        ce = nn.BCEWithLogitsLoss(reduction="none")(logits, targets)
        p_t = targets * prob + (1 - targets) * (1 - prob)
        loss = self.alpha * (1 - p_t) ** self.gamma * ce
        return loss.mean()

# ===================== 文本预处理 =====================
def raw_text_clean(text: str) -> str:
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9\s，。、：~-]', '', text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def text_augment(text: str) -> str:
    punc = [" ", "，", "、", "~", "-"]
    if random.random() < 0.4:
        text += random.choice(punc)
    return text

# ===================== 动态词库 =====================
def build_dynamic_vocab(all_texts, vocab_limit):
    char_freq = {}
    for text in all_texts:
        for ch in text:
            char_freq[ch] = char_freq.get(ch, 0) + 1
    sorted_chars = sorted(char_freq.keys(), key=lambda x: char_freq[x], reverse=True)
    keep_chars = sorted_chars[:vocab_limit - 2]
    vocab = {"<PAD>": 0, "<UNK>": 1}
    for idx, char in enumerate(keep_chars, start=2):
        vocab[char] = idx
    return vocab

# ===================== 数据集 =====================
class ExtractDataset(Dataset):
    def __init__(self, text_list, label_list, dynamic_vocab):
        self.text_list = text_list
        self.label_list = label_list
        self.vocab = dynamic_vocab

    def __len__(self):
        return len(self.text_list)

    def __getitem__(self, idx):
        text = self.text_list[idx]
        label = self.label_list[idx]
        text = raw_text_clean(text)
        text = text_augment(text)
        tokens = [self.vocab.get(c, self.vocab["<UNK>"]) for c in text]
        if len(tokens) > MAX_SEQ_LEN:
            tokens = tokens[:MAX_SEQ_LEN]
        return torch.tensor(tokens, dtype=torch.long), torch.tensor(label, dtype=torch.float)

# ===================== 模型（修复：补上最终分类层输出CLASS_NUM维） =====================
class EyeModel(nn.Module):
    def __init__(self, vocab_size, embed_dim, class_num, dropout):
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, embed_dim, padding_idx=0)
        self.conv1 = nn.Conv1d(embed_dim, 96, kernel_size=3, padding=1)
        self.ln1 = nn.LayerNorm(96)
        self.conv2 = nn.Conv1d(96, 128, kernel_size=3, padding=1)
        self.ln2 = nn.LayerNorm(128)
        self.conv3 = nn.Conv1d(128, 80, kernel_size=5, padding=2)
        self.ln3 = nn.LayerNorm(80)
        self.conv4 = nn.Conv1d(80, 32, kernel_size=5, padding=2)
        self.ln4 = nn.LayerNorm(32)

        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(dropout)

        self.gru = nn.GRU(
            input_size=32,
            hidden_size=80,
            num_layers=2,
            bidirectional=True,
            batch_first=True,
            dropout=dropout
        )
        self.pool = nn.AdaptiveMaxPool1d(1)
        # 最终输出 8 维
        self.fc = nn.Linear(160, class_num)

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
        # 输出分类 logits
        return self.fc(feat)

# ===================== 批次对齐 =====================
def collate_fn(batch):
    tokens_list, label_list = zip(*batch)
    max_len = min(max(len(t) for t in tokens_list), MAX_SEQ_LEN)
    pad_tokens = []
    for t in tokens_list:
        if len(t) < max_len:
            pad_t = torch.cat([t, torch.zeros(max_len - len(t), dtype=torch.long)])
        else:
            pad_t = t[:max_len]
        pad_tokens.append(pad_t)
    tokens = torch.stack(pad_tokens)
    labels = torch.stack(label_list)
    return tokens, labels

# ===================== 训练&验证 =====================
def train_one_epoch(model, loader, loss_fn, optimizer):
    model.train()
    total_loss = 0.0
    total_correct = 0
    total_elem = 0
    for tokens, labels in loader:
        tokens, labels = tokens.to(device), labels.to(device)
        optimizer.zero_grad()
        logits = model(tokens)
        loss = loss_fn(logits, labels)
        loss.backward()
        optimizer.step()

        total_loss += loss.item()
        prob = torch.sigmoid(logits)
        pred = (prob > 0.5).float()
        total_correct += (pred == labels).sum().item()
        total_elem += labels.numel()

    acc = total_correct / total_elem
    return total_loss / len(loader), acc

def val_one_epoch(model, loader, loss_fn):
    model.eval()
    total_loss = 0.0
    total_correct = 0
    total_elem = 0
    with torch.no_grad():
        for tokens, labels in loader:
            tokens, labels = tokens.to(device), labels.to(device)
            logits = model(tokens)
            loss = loss_fn(logits, labels)
            total_loss += loss.item()
            prob = torch.sigmoid(logits)
            pred = (prob > 0.5).float()
            total_correct += (pred == labels).sum().item()
            total_elem += labels.numel()

    acc = total_correct / total_elem
    return total_loss / len(loader), acc

# ===================== 主程序 =====================
if __name__ == "__main__":
    # 加载数据
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    text_list = []
    label_list = []
    key2idx = {"岗位名称": 0, "薪资": 1, "工作地点": 2}

    for item in raw_data:
        text = item["raw"]
        key = item["key"]
        label = [0.0] * CLASS_NUM
        if key in key2idx:
            label[key2idx[key]] = 1.0
        text_list.append(text)
        label_list.append(label)

    # 生成动态词库
    dynamic_vocab = build_dynamic_vocab(text_list, VOCAB_SIZE)
    with open(VOCAB_SAVE_PATH, "w", encoding="utf-8") as f:
        json.dump(dynamic_vocab, f, ensure_ascii=False, indent=2)
    print(f"动态词库已保存: {VOCAB_SAVE_PATH}")

    # 划分数据集
    train_txt, val_txt, train_lab, val_lab = train_test_split(
        text_list, label_list, test_size=0.2, random_state=42
    )

    train_set = ExtractDataset(train_txt, train_lab, dynamic_vocab)
    val_set = ExtractDataset(val_txt, val_lab, dynamic_vocab)
    train_loader = DataLoader(train_set, batch_size=BATCH_SIZE, shuffle=True, collate_fn=collate_fn)
    val_loader = DataLoader(val_set, batch_size=BATCH_SIZE, shuffle=False, collate_fn=collate_fn)

    # 初始化
    loss_fn = FocalLoss(alpha=1.2, gamma=2.0)
    model = EyeModel(VOCAB_SIZE, EMBED_DIM, CLASS_NUM, DROPOUT_RATE).to(device)
    optimizer = optim.Adam(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)
    scheduler = StepLR(optimizer, step_size=15, gamma=0.85)

    # 训练循环 + 早停（连续10轮验证损失不下降即停止）
    best_val_loss = float("inf")
    patience_count = 0
    print("\n===== 长短文本通用信息抽取模型 开始训练 =====")

    for epoch in range(1, MAX_EPOCH + 1):
        train_loss, train_acc = train_one_epoch(model, train_loader, loss_fn, optimizer)
        val_loss, val_acc = val_one_epoch(model, val_loader, loss_fn)
        scheduler.step()

        print(f"Epoch {epoch:3d} | TrainLoss:{train_loss:.4f} | ValLoss:{val_loss:.4f} | TrainAcc:{train_acc:.4f} | ValAcc:{val_acc:.4f}")

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            patience_count = 0
            torch.save(model.state_dict(), MODEL_WEIGHT_PATH)
        else:
            patience_count += 1
            # 连续10轮无提升，触发早停
            if patience_count >= PATIENCE:
                print(f"\n连续{PATIENCE}轮验证损失无下降，触发早停，训练结束")
                break

    print(f"\n训练完成，最优验证损失: {best_val_loss:.4f}")