# -*- coding: utf-8 -*-
# Автор: Гусев Илья

from typing import List, Tuple
import os
import copy

import numpy as np
from keras.layers import Input, Embedding, Dense, Dropout, Reshape, TimeDistributed
from keras.models import Model, model_from_yaml
from keras.optimizers import Adam
from keras.callbacks import EarlyStopping
from keras import backend as K

from rnnmorph.batch_generator import CHAR_SET
from rnnmorph.data_preparation.word_vocabulary import WordVocabulary


def build_dense_chars_layer(max_word_length, char_vocab_size, char_emb_dim,
                            hidden_dim, output_dim, dropout):
    chars_embedding_layer = Embedding(char_vocab_size, char_emb_dim, name='chars_embeddings')
    chars_dense_1 = Dense(hidden_dim, activation='relu')
    chars_dense_2 = Dense(output_dim)

    def dense_layer(inp):
        if len(K.int_shape(inp)) == 3:
            chars_embedding = TimeDistributed(chars_embedding_layer)(inp)
            chars_embedding = Reshape((-1, char_emb_dim * max_word_length))(chars_embedding)
        elif len(K.int_shape(inp)) == 2:
            chars_embedding = chars_embedding_layer(inp)
            chars_embedding = Reshape((char_emb_dim * max_word_length,))(chars_embedding)
        else:
            assert False
        chars_embedding = Dropout(dropout)(chars_embedding)
        chars_embedding = Dropout(dropout)(chars_dense_1(chars_embedding))
        chars_embedding = Dropout(dropout)(chars_dense_2(chars_embedding))
        return chars_embedding

    return dense_layer


class CharEmbeddingsModel:
    def __init__(self):
        self.model = None  # type: Model
        self.char_layer = None

    def save(self, model_config_path: str, model_weights_path: str):
        with open(model_config_path, "w", encoding='utf-8') as f:
            f.write(self.model.to_yaml())
        self.model.save_weights(model_weights_path)

    def load(self, model_config_path: str, model_weights_path: str) -> None:
        with open(model_config_path, "r", encoding='utf-8') as f:
            self.model = model_from_yaml(f.read())
        self.model.load_weights(model_weights_path)
        self.char_layer = TimeDistributed(Model(self.model.input_layers[0].output, self.model.layers[-2].input))

    def build(self,
              char_layer,
              vocabulary_size: int,
              word_embeddings_dimension: int,
              max_word_length: int,
              word_embeddings: np.array):
        self.char_layer = char_layer
        chars = Input(shape=(max_word_length, ), name='chars')

        output = Dense(vocabulary_size, weights=[word_embeddings], use_bias=False,
                       trainable=False, activation='softmax')
        output = output(Dense(word_embeddings_dimension, name='char_embed_to_word_embed')(self.char_layer(chars)))

        self.model = Model(inputs=chars, outputs=output)
        self.model.compile(loss='sparse_categorical_crossentropy', optimizer=Adam())
        print(self.model.summary())

    def train(self,
              vocabulary: WordVocabulary,
              val_part: float,
              random_seed: int,
              batch_size: int,
              max_word_len: int) -> None:
        """
        Обучение модели.

        :param vocabulary: список слов.
        :param val_part: на какой части выборки оценивать качество.
        :param random_seed: зерно для случайного генератора.
        :param batch_size: размер батча.
        :param max_word_len: максимальный учитываемый размер слова.
        """
        np.random.seed(random_seed)
        chars, y = self.prepare_words(vocabulary, max_word_len)
        callbacks = [EarlyStopping(patience=2)]
        self.model.fit(chars, y, batch_size=batch_size, epochs=100, verbose=2,
                       validation_split=val_part, callbacks=callbacks)

    @staticmethod
    def prepare_words(vocabulary, max_word_length):
        chars = np.zeros((vocabulary.size(), max_word_length), dtype=np.int)
        y = np.zeros((vocabulary.size(), ), dtype=np.int)
        for i in range(vocabulary.size()):
            y[i] = i
        for i, word in enumerate(vocabulary.words):
            word_char_indices = [CHAR_SET.index(ch) if ch in CHAR_SET else len(CHAR_SET) for ch in word][:max_word_length]
            chars[i, -min(len(word), max_word_length):] = word_char_indices
        return chars, y


def get_char_model(
        char_layer,
        max_word_length,
        vocabulary: WordVocabulary,
        embeddings,
        model_weights_path: str,
        model_config_path: str,
        batch_size: int=128,
        val_part: float=0.2,
        seed: int=42):
    """
    :param embeddings: матрица эмбеддингов
    :param batch_size: размер батча.
    :param model_weights_path: путь, куда сохранять веса модели.
    :param model_config_path: путь, куда сохранять конфиг модели.
    :param val_part: доля val выборки.
    :param seed: seed для ГПСЧ
    """
    model = CharEmbeddingsModel()
    if model_config_path is not None and os.path.exists(model_config_path):
        assert model_weights_path is not None and os.path.exists(model_weights_path)
        model.load(model_config_path, model_weights_path)
    else:
        vocabulary = copy.deepcopy(vocabulary)
        vocabulary.shrink(embeddings.shape[0])
        model.build(vocabulary_size=vocabulary.size(),
                    word_embeddings_dimension=embeddings.shape[1],
                    max_word_length=max_word_length,
                    word_embeddings=embeddings.T,
                    char_layer=char_layer)
        model.train(vocabulary, val_part, seed, batch_size, max_word_length)
        if model_config_path is not None and model_weights_path is not None:
            model.save(model_config_path, model_weights_path)
    return model.char_layer
