# -*- coding: utf-8 -*-
# Автор: Гусев Илья
# Описание: Модель PoS-теггера на основе BiLSTM.

from typing import List, Tuple
import os

import numpy as np
import pymorphy2
from keras.layers import Input, Embedding, Dense, LSTM, BatchNormalization, Activation, \
    concatenate, Bidirectional, TimeDistributed, Dropout, Flatten, Reshape
from keras.models import Model, model_from_yaml
from keras.optimizers import Adam

from rnnmorph.batch_generator import BatchGenerator, CHAR_SET
from rnnmorph.data_preparation.grammeme_vectorizer import GrammemeVectorizer
from rnnmorph.data_preparation.word_vocabulary import WordVocabulary
from rnnmorph.data_preparation.loader import Loader
from rnnmorph.char_embeddings_model import build_dense_chars_layer, get_char_model
from rnnmorph.config import BuildModelConfig, TrainConfig


class LSTMMorphoAnalysis:
    def __init__(self):
        self.morph = pymorphy2.MorphAnalyzer()  # type: pymorphy2.MorphAnalyzer
        self.grammeme_vectorizer_input = GrammemeVectorizer()  # type: GrammemeVectorizer
        self.grammeme_vectorizer_output = GrammemeVectorizer()  # type: GrammemeVectorizer
        self.word_vocabulary = WordVocabulary()  # type: WordVocabulary
        self.model = None  # type: Model

    def prepare(self, gram_dump_path_input: str, gram_dump_path_output: str,
                word_vocabulary_dump_path: str, file_names: List[str] = None) -> None:
        """
        Подготовка векторизатора грамматических значений и словаря слов по корпусу.
        """
        if os.path.exists(gram_dump_path_input):
            self.grammeme_vectorizer_input.load(gram_dump_path_input)
        if os.path.exists(gram_dump_path_output):
            self.grammeme_vectorizer_output.load(gram_dump_path_output)
        if os.path.exists(word_vocabulary_dump_path):
            self.word_vocabulary.load(word_vocabulary_dump_path)
        if self.grammeme_vectorizer_input.is_empty() or \
                self.grammeme_vectorizer_output.is_empty() or \
                self.word_vocabulary.is_empty():
            self.grammeme_vectorizer_input, self.grammeme_vectorizer_output, self.word_vocabulary = \
                Loader().parse_corpora(file_names)
            self.grammeme_vectorizer_input.save(gram_dump_path_input)
            self.grammeme_vectorizer_output.save(gram_dump_path_output)
            self.word_vocabulary.save(word_vocabulary_dump_path)

    def save(self, model_config_path: str, model_weights_path: str):
        with open(model_config_path, "w", encoding='utf-8') as f:
            f.write(self.model.to_yaml())
        self.model.save_weights(model_weights_path)

    def load(self, model_config_path: str, model_weights_path: str) -> None:
        with open(model_config_path, "r", encoding='utf-8') as f:
            self.model = model_from_yaml(f.read())
        self.model.load_weights(model_weights_path)
        self.model.compile(loss='sparse_categorical_crossentropy', optimizer=Adam(), metrics=['accuracy'])

    def build(self, config: BuildModelConfig, word_embeddings=None):
        """
        Описание модели.

        :param config: конфиг модели.
        :param word_embeddings: матрица словных эмбеддингов.
        """
        inputs = []
        embeddings = []

        if config.use_word_embeddings and word_embeddings is not None:
            words = Input(shape=(None,), name='words')
            word_vocabulary_size = word_embeddings.size.shape[0]
            word_embeddings_dim = word_embeddings.size.shape[1]
            words_embedding = Embedding(word_vocabulary_size, word_embeddings_dim, name='word_embeddings')(words)
            embeddings.append(words_embedding)

        if config.use_gram:
            grammemes_input = Input(shape=(None, self.grammeme_vectorizer_input.grammemes_count()), name='grammemes')
            grammemes_embedding = Dropout(config.gram_dropout)(grammemes_input)
            grammemes_embedding = Dense(config.gram_hidden_size, activation='relu')(grammemes_embedding)
            inputs.append(grammemes_input)
            embeddings.append(grammemes_embedding)

        if config.use_chars:
            chars_input = Input(shape=(None, config.char_max_word_length), name='chars')

            char_layer = build_dense_chars_layer(
                max_word_length=config.char_max_word_length,
                char_vocab_size=len(CHAR_SET)+1,
                char_emb_dim=config.char_embedding_dim,
                hidden_dim=config.char_function_hidden_size,
                output_dim=config.char_function_output_size,
                dropout=config.char_dropout)
            if config.use_trained_char_embeddings:
                char_layer = get_char_model(
                    char_layer=char_layer,
                    max_word_length=config.char_max_word_length,
                    embeddings=word_embeddings,
                    model_config_path=config.char_model_config_path,
                    model_weights_path=config.char_model_weights_path,
                    vocabulary=self.word_vocabulary)
            chars_embedding = char_layer(chars_input)
            inputs.append(chars_input)
            embeddings.append(chars_embedding)

        if len(embeddings) > 1:
            layer = concatenate(embeddings, name="LSTM_input")
        else:
            layer = embeddings[0]
        for i in range(config.rnn_n_layers):
            layer = Bidirectional(LSTM(
                config.rnn_hidden_size,
                dropout=config.rnn_dropout,
                recurrent_dropout=config.rnn_dropout,
                return_sequences=True,
                name='LSTM_'+str(i)))(layer)

        layer = TimeDistributed(Dense(config.dense_size))(layer)
        layer = TimeDistributed(Dropout(config.dense_dropout))(layer)
        layer = TimeDistributed(BatchNormalization())(layer)
        layer = TimeDistributed(Activation('relu'))(layer)

        outputs = []
        num_of_classes = self.grammeme_vectorizer_output.size() + 1
        print(num_of_classes)
        if config.use_crf:
            from keras_contrib.layers import CRF
            crf_layer = CRF(num_of_classes, sparse_target=True, name='crf')
            outputs.append(crf_layer(layer))
            self.model = Model(inputs=inputs, outputs=outputs)
            loss = {'crf': crf_layer.loss_function}
            metrics = {'crf': crf_layer.accuracy}
            self.model.compile(Adam(clipnorm=5.), loss=loss, metrics=metrics)
        else:
            outputs.append(TimeDistributed(Dense(num_of_classes, activation='softmax'))(layer))
            self.model = Model(inputs=inputs, outputs=outputs)
            self.model.compile(loss='sparse_categorical_crossentropy', optimizer=Adam(), metrics=['accuracy'])
        print(self.model.summary())

    def train(self, file_names: List[str], train_config: TrainConfig, build_config: BuildModelConfig) -> None:
        np.random.seed(train_config.random_seed)
        sample_counter = self.count_samples(file_names)
        train_idx, val_idx = self.get_split(sample_counter, train_config.val_part)
        for big_epoch in range(train_config.epochs_num):
            print('------------Big Epoch {}------------'.format(big_epoch))
            batch_generator = BatchGenerator(
                    file_names=file_names,
                    config=train_config,
                    grammeme_vectorizer_input=self.grammeme_vectorizer_input,
                    grammeme_vectorizer_output=self.grammeme_vectorizer_output,
                    max_word_len=build_config.char_max_word_length,
                    indices=train_idx,
                    word_vocabulary=self.word_vocabulary,
                    word_count=build_config.word_max_count)
            for epoch, (words, grammemes, chars, y) in enumerate(batch_generator):
                max_sentence_length = grammemes.shape[1]
                batch_size = train_config.num_words_in_batch // int(max_sentence_length)
                inputs = []
                if build_config.use_word_embeddings:
                    inputs.append(words)
                if build_config.use_gram:
                    inputs.append(grammemes)
                if build_config.use_chars:
                    inputs.append(chars)
                self.model.fit(inputs, y, batch_size=batch_size, epochs=1, verbose=2)
                if epoch != 0 and epoch % train_config.dump_model_freq == 0:
                    self.save(train_config.model_config_path, train_config.model_weights_path)
            self.evaluate(
                filenames=file_names,
                val_idx=val_idx,
                config=train_config,
                max_word_len=build_config.char_max_word_length,
                max_word_count=build_config.word_max_count)

    @staticmethod
    def count_samples(file_names: List[str]):
        """
        Считает количество предложений в выборке.
        :param file_names: файлы выборки.
        :return: количество предложений.
        """
        sample_counter = 0
        for filename in file_names:
            with open(filename, "r", encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if len(line) == 0:
                        sample_counter += 1
        return sample_counter

    @staticmethod
    def get_split(sample_counter: int, val_part: float) -> Tuple[np.array, np.array]:
        """
        Выдаёт индексы предложений, которые становятся train или val выборкой.
        :param sample_counter: количество предложений.
        :param val_part: часть выборки, которая станет val.
        :return: индексы выборок.
        """
        perm = np.random.permutation(sample_counter)
        border = int(sample_counter * (1 - val_part))
        train_idx = perm[:border]
        val_idx = perm[border:]
        return train_idx, val_idx

    def evaluate(self, filenames, val_idx, config: TrainConfig, max_word_len: int, max_word_count: int) -> None:
        """
        Оценка на val выборке.
        :param filenames: файлы выборки.
        :param val_idx: val индексы.
        :param config: конфиг обучения
        :param max_word_len: максимальный учитываемый размер слова.
        """
        word_count = 0
        word_errors = 0
        sentence_count = 0
        sentence_errors = 0
        batch_generator = BatchGenerator(
            file_names=filenames,
            config=config,
            grammeme_vectorizer_input=self.grammeme_vectorizer_input,
            grammeme_vectorizer_output=self.grammeme_vectorizer_output,
            max_word_len=max_word_len,
            indices=val_idx,
            word_vocabulary=self.word_vocabulary,
            word_count=max_word_count)
        for epoch, (grammemes, chars, y) in enumerate(batch_generator):
            max_sentence_length = grammemes.shape[1]
            batch_size = config.num_words_in_batch // max_sentence_length
            predicted_y = self.model.predict([grammemes, chars], batch_size=batch_size, verbose=0)
            for i, sentence in enumerate(y):
                sentence_has_errors = False
                count_zero = sum([1 for num in sentence if num == [0]])
                real_sentence_tags = sentence[count_zero:]
                answer = []
                for grammeme_probs in predicted_y[i][count_zero:]:
                    num = np.argmax(grammeme_probs)
                    answer.append(num)
                for tag, predicted_tag in zip(real_sentence_tags, answer):
                    tag = tag[0]
                    word_count += 1
                    if tag != predicted_tag:
                        word_errors += 1
                        sentence_has_errors = True
                sentence_count += 1
                if sentence_has_errors:
                    sentence_errors += 1

        print("Word accuracy: ", 1.0 - float(word_errors) / word_count)
        print("Sentence accuracy: ", 1.0 - float(sentence_errors) / sentence_count)

    def predict_proba(self, sentences: List[List[str]], max_word_len: int=30,
                      word_max_count: int=10000) -> List[List[List[float]]]:
        """
        Предсказание полных PoS-тегов по предложению с вероятностями всех вариантов.
        :param sentences: массив предложений (которые являются массивом слов).
        :param max_word_len: максимальный учитываемый размер слова.
        :return: вероятности тегов.
        """
        max_sentence_len = max([len(sentence) for sentence in sentences])
        n_samples = len(sentences)
        grammemes = np.zeros((n_samples, max_sentence_len, self.grammeme_vectorizer_input.grammemes_count()),
                             dtype=np.float)
        chars = np.zeros((n_samples, max_sentence_len, max_word_len), dtype=np.int)

        for i, sentence in enumerate(sentences):
            gram_vectors, char_vectors = BatchGenerator.get_sample(
                sentence,
                morph=self.morph,
                grammeme_vectorizer=self.grammeme_vectorizer_input,
                max_word_len=max_word_len,
                word_vocabulary=self.word_vocabulary,
                word_count=word_max_count)
            grammemes[i, -len(sentence):] = gram_vectors
            chars[i, -len(sentence):] = char_vectors

        return self.model.predict([grammemes, chars])

    def predict(self, sentences: List[List[str]]) -> List[List[int]]:
        """
        Предсказание полных PoS-тегов по предложению.
        :param sentences: массив предложений (которые являются массивом слов).
        :return: массив тегов.
        """
        answers = []
        for sentence, probs in zip(sentences, self.predict_proba(sentences)):
            answer = []
            for grammeme_probs in probs[-len(sentence):]:
                num = np.argmax(grammeme_probs[1:])
                answer.append(num)
            answers.append(answer)
        return answers
