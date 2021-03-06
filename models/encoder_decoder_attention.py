import random
import numpy as np
import tensorflow as tf
from tensorflow.keras.models import Model
from tensorflow.keras.layers \
    import Dense, Embedding, LSTM, GRU, TimeDistributed
from tensorflow.keras.losses \
    import sparse_categorical_crossentropy, categorical_crossentropy
from tensorflow.keras.preprocessing.sequence import pad_sequences
from utils.datasets.small_parallel_enja import load_small_parallel_enja
from utils.preprocessing.sequence import sort
from sklearn.utils import shuffle
from layers import Attention


class Encoder(Model):
    def __init__(self,
                 input_dim,
                 hidden_dim):
        super().__init__()
        self.embedding = Embedding(input_dim, hidden_dim, mask_zero=True)
        self.lstm = LSTM(hidden_dim, return_state=True, return_sequences=True)

    def call(self, x):
        x = self.embedding(x)
        y, state_h, state_c = self.lstm(x)

        return y, (state_h, state_c)


class Decoder(Model):
    def __init__(self,
                 hidden_dim,
                 output_dim):
        super().__init__()
        self.embedding = Embedding(output_dim, hidden_dim, mask_zero=True)
        self.lstm = LSTM(hidden_dim, return_state=True, return_sequences=True)
        self.attn = Attention(hidden_dim, hidden_dim)
        self.out = Dense(output_dim, activation='softmax')

    def call(self, target, hs, states, source=None):
        x = self.embedding(target)
        x, state_h, state_c = self.lstm(x, states)
        x = self.attn(x, hs, source=source)
        y = self.out(x)

        return y, (state_h, state_c)


class EncoderDecoder(Model):
    def __init__(self,
                 input_dim,
                 hidden_dim,
                 output_dim,
                 bos_value=1,
                 max_len=20):
        super().__init__()
        self.encoder = Encoder(input_dim, hidden_dim)
        self.decoder = Decoder(hidden_dim, output_dim)

        self._BOS = bos_value
        self._max_len = max_len

    def call(self, source, target=None, use_teacher_forcing=False):
        batch_size = len(source)
        if target is not None:
            len_target_sequences = len(target[0])
        else:
            len_target_sequences = self._max_len

        hs, states = self.encoder(source)

        y = tf.ones((batch_size, 1)) * self._BOS
        output = []

        for t in range(len_target_sequences):
            out, states = self.decoder(y, hs, states, source=source)
            output.append(out[:, 0])

            if use_teacher_forcing and target is not None:
                y = target[:, t][:, tf.newaxis]
            else:
                y = tf.argmax(out, axis=-1)

        output = tf.convert_to_tensor(output, dtype=tf.float32)
        output = tf.transpose(output, perm=[1, 0, 2])

        return output


def compute_loss(label, pred, from_logits=False):
    return categorical_crossentropy(label, pred,
                                    from_logits=from_logits)


def train_step(x, t, depth_t,
               teacher_forcing_rate=0.5,
               pad_value=0):
    use_teacher_forcing = (random.random() < teacher_forcing_rate)
    with tf.GradientTape() as tape:
        preds = model(x, t, use_teacher_forcing=use_teacher_forcing)
        label = tf.one_hot(t, depth=depth_t, dtype=tf.float32)
        mask_t = tf.cast(tf.not_equal(t, pad_value), tf.float32)
        label = label * mask_t[:, :, tf.newaxis]
        loss = compute_loss(label, preds)
    grads = tape.gradient(loss, model.trainable_variables)
    optimizer.apply_gradients(zip(grads, model.trainable_variables))

    return loss.numpy(), preds.numpy()


def valid_step(x, t, depth_t, pad_value=0):
    preds = model(x, t, use_teacher_forcing=False)
    label = tf.one_hot(t, depth=depth_t, dtype=tf.float32)
    mask_t = tf.cast(tf.not_equal(t, pad_value), tf.float32)
    label = label * mask_t[:, :, tf.newaxis]
    loss = compute_loss(label, preds)

    return loss.numpy(), preds.numpy()


def test_step(x):
    preds = model(x)
    return preds.numpy()


def ids_to_sentence(ids, i2w):
    return [i2w[id] for id in ids]


if __name__ == '__main__':
    np.random.seed(1234)

    '''
    Load data
    '''
    (x_train, y_train), \
        (x_test, y_test), \
        (num_x, num_y), \
        (w2i_x, w2i_y), (i2w_x, i2w_y) = \
        load_small_parallel_enja(to_ja=True, add_bos=False)

    N = len(x_train)
    train_size = int(N * 0.8)
    valid_size = N - train_size
    (x_train, y_train), (x_valid, y_valid) = \
        (x_train[:train_size], y_train[:train_size]), \
        (x_train[train_size:], y_train[train_size:])

    x_train, y_train = sort(x_train, y_train)
    x_valid, y_valid = sort(x_valid, y_valid)
    x_test, y_test = sort(x_test, y_test)

    train_size = 40000
    valid_size = 200
    test_size = 10
    x_train, y_train = x_train[:train_size], y_train[:train_size]
    x_valid, y_valid = x_valid[:valid_size], y_valid[:valid_size]
    x_test, y_test = x_test[:test_size], y_test[:test_size]

    '''
    Build model
    '''

    input_dim = num_x
    hidden_dim = 256
    output_dim = num_y

    model = EncoderDecoder(input_dim, hidden_dim, output_dim)
    optimizer = tf.keras.optimizers.Adam()

    '''
    Train model
    '''
    epochs = 20
    batch_size = 100
    n_batches = len(x_train) // batch_size

    for epoch in range(epochs):
        print('-' * 20)
        print('Epoch: {}'.format(epoch+1))
        train_loss = 0.

        for batch in range(n_batches):
            start = batch * batch_size
            end = start + batch_size

            _x_train = pad_sequences(x_train[start:end], padding='post')
            _y_train = pad_sequences(y_train[start:end], padding='post')

            loss, _ = train_step(_x_train, _y_train, num_y)
            train_loss += loss.sum()

        train_loss = train_loss / train_size

        _x_valid = pad_sequences(x_valid, padding='post')
        _y_valid = pad_sequences(y_valid, padding='post')
        valid_loss, preds = valid_step(_x_valid, _y_valid, num_y)
        valid_loss = valid_loss.sum() / valid_size
        print('Valid loss: {:.3}'.format(valid_loss))

        for i, source in enumerate(x_test):
            out = test_step(np.array(source)[np.newaxis, :])[0]
            out = tf.argmax(out, axis=-1).numpy()
            out = ' '.join(ids_to_sentence(out, i2w_y))
            source = ' '.join(ids_to_sentence(source, i2w_x))
            target = ' '.join(ids_to_sentence(y_test[i], i2w_y))
            print('>', source)
            print('=', target)
            print('<', out)
            print()
