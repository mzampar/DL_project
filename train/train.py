import pandas as pd
import matplotlib.pyplot as plt
import os
import time

import torch as th
from torch.utils.data import Dataset
from torch import nn
from torch.optim.lr_scheduler import StepLR

from ConvLSTM_model import ConvLSTM_Model

import torch.nn.functional as F
from pytorch_msssim import ssim

class HybridLoss(nn.Module):
    def __init__(self, alpha=0.5):
        """
        Initialize hybrid loss with a weighting factor alpha for SSIM.
        :param alpha: Weight for SSIM loss; (1 - alpha) is the weight for MSE loss.
        """
        super(HybridLoss, self).__init__()
        self.alpha = alpha
        self.mse_loss = nn.MSELoss()

    def forward(self, pred, target):
        mse = self.mse_loss(pred, target)
        ssim_loss = 1 - ssim(pred, target, data_range=1.0, size_average=True)
        return (1 - self.alpha) * mse + self.alpha * ssim_loss

class SequenceDataset(th.utils.data.Dataset):
    def __init__(self, input_data, tensor_dir, k_in=10, k_out=10):
        self.input_data = input_data
        self.tensor_dir = tensor_dir
        self.k_in = k_in # Number of frames to be considered
        self.k_out = k_out

    def __getitem__(self, index):
        # Get the row using the index
        row = self.input_data.iloc[index]
        # Get the sequence
        in_seq = row.iloc[:self.k_in]
        in_seq_tensor = th.stack([th.load(os.path.join(self.tensor_dir, f"tensor_{frame}.pt"), weights_only=True) for frame in in_seq])
        out_seq = row.iloc[self.k_in:self.k_in+self.k_out]
        out_seq_tensor = th.stack([th.load(os.path.join(self.tensor_dir, f"tensor_{frame}.pt"), weights_only=True) for frame in out_seq])

        return in_seq_tensor, out_seq_tensor

    def __len__(self):
        return self.input_data.shape[0]


id_data = pd.read_csv('../data/id_df_final_10.csv')

seq_len = id_data.groupby('sequence').size()
seq_len = seq_len.to_dict()
seq_rain = id_data.groupby('sequence')['rain_category'].mean()
seq_rain = seq_rain.to_dict()

seq_df = pd.DataFrame({'seq_len': seq_len, 'seq_rain': seq_rain})

# split the sequences in train and test set (80/20)
train_seq = seq_df.sample(frac=0.8, random_state=1)
test_seq = seq_df.drop(train_seq.index)

# get the sequences of the train and test set
train_seq_idx = train_seq.index
test_seq_idx = test_seq.index
train_data = id_data[id_data['sequence'].isin(train_seq_idx)]
dataset = pd.read_csv('../data/id_seq_dataset_10.csv')
train_data = dataset[dataset['seq_id'].isin(train_seq_idx)]
test_data = dataset[dataset['seq_id'].isin(test_seq_idx)]

# exclude the sequence and rain column
train_data = train_data.drop(columns=['seq_id', 'rain_category'])
test_data = test_data.drop(columns=['seq_id', 'rain_category'])
# convert the df to int
train_data = train_data.astype(int)
test_data = test_data.astype(int)

train_dataset = SequenceDataset(train_data, '../../fast/gray_tensor/', k_in=5, k_out=5)
test_dataset = SequenceDataset(test_data, '../../fast/gray_tensor/', k_in=5, k_out=5)

id_data = None
seq_df = None
train_data = None
test_data = None
dataset = None

batch_size = 64
# model
filter_size = 5
stride = 1
patch_size = 2
layer_norm = 0

num_hidden = [64, 32, 32, 64]
num_layers = len(num_hidden)

custom_model_config = {
    'in_shape': [5, 1, 128, 128], # T, C, H, W
    'patch_size': 1,
    'filter_size': 1, # given to ConvLSTMCell
    'stride': 1, # given to ConvLSTMCell
    'layer_norm' : False, # given to ConvLSTMCell
    # the sum of pre_seq_length and aft_seq_length has to be = len(inputs)
    'pre_seq_length': 3,
    'aft_seq_length': 2,
    'target_seq_length': 5,
    'reverse_scheduled_sampling': 0,
    'batch_size': batch_size
}

if th.cuda.is_available():
    print("CUDA is available!")
else:
    print("CUDA is not available.")

device = th.device("cuda" if th.cuda.is_available() else "cpu")
th.cuda.empty_cache()

# Instantiate the model
# Assuming x_train shape is (batch_size, sequence_length, channels, height, width)
model = ConvLSTM_Model(num_layers, num_hidden, custom_model_config)
model.to(device)

dataloader = th.utils.data.DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
test_dataloader = th.utils.data.DataLoader(test_dataset, batch_size=batch_size, shuffle=False)

# Define loss and optimizer
#criterion = nn.MSELoss()
criterion = HybridLoss(alpha=0.25)
optimizer = th.optim.Adam(model.parameters(), lr=1)
scheduler = StepLR(optimizer, step_size=1, gamma=0.1)

# Training loop
num_epochs = 10  # Set the number of epochs

# Lists to keep track of the losses for each epoch
train_losses = []
test_losses = []

print("Starting training loop...")

start_time = time.time()

for epoch in range(num_epochs):
    # Training phase
    model.train()
    running_loss = 0.0
    for batch_idx, (inputs, targets) in enumerate(dataloader):
        # Move data to device (GPU if available)
        inputs, targets = inputs.to(device), targets.to(device)
        # reduce smoothly the weight of the mask to make the model able to use its own predictions also and not only the inputs when analysing the given sequence 
        mask_true = th.ones(inputs.shape).to(device)
        # Zero the parameter gradients
        optimizer.zero_grad()
        # Forward pass
        outputs = model(inputs, mask_true)
        # Compute loss
        loss = criterion(outputs, targets)
        # Backward pass and optimize
        loss.backward()
        optimizer.step()
        # Accumulate loss
        running_loss += loss.item()
        
        # Print training info every 10 batches
        if batch_idx % 10 == 0:
            print(f"Epoch [{epoch+1}/{num_epochs}], Batch [{batch_idx+1}], Loss: {loss.item():.4f}")

    scheduler.step()
    # Calculate and store the average training loss for this epoch
    epoch_train_loss = running_loss / len(dataloader)
    train_losses.append(epoch_train_loss)
    print(f"Epoch [{epoch+1}/{num_epochs}] - Average Train Loss: {epoch_train_loss:.4f}")

    # Validation (test) phase
    model.eval()
    test_loss = 0.0
    with th.no_grad():  # No gradients needed for testing
        for batch_idx, (inputs, targets) in enumerate(test_dataloader):
            inputs, targets = inputs.to(device), targets.to(device)
            mask_true = th.ones(inputs.shape).to(device)
            outputs = model(inputs, mask_true)
            loss = criterion(outputs, targets)
            test_loss += loss.item()

    # Calculate and store the average test loss for this epoch
    epoch_test_loss = test_loss / len(test_dataloader)  # Using len(test_dataloader) for batch average
    test_losses.append(epoch_test_loss)
    print(f"Epoch [{epoch+1}/{num_epochs}] - Average Test Loss: {epoch_test_loss:.4f}")

end_time = time.time()

print(f"Training completed in {end_time - start_time}!")

th.save(model.state_dict(), "../models/model.pth")

print("Model saved!")

# Plot the training and test losses
plt.plot(train_losses, label='Train Loss')
plt.plot(test_losses, label='Test Loss')
plt.xlabel('Epoch')
plt.ylabel('Loss')
plt.legend()
plt.savefig('loss_plot.png')