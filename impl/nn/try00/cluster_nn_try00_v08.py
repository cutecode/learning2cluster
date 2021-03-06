from keras.layers import Reshape, Concatenate, Bidirectional, LSTM, Dense, BatchNormalization, \
    Activation, Lambda, TimeDistributed
from keras.layers.advanced_activations import LeakyReLU
import keras.backend as K

import numpy as np

from core.nn.helper import slice_layer, get_cluster_centers, get_cluster_cohesion, get_cluster_separation
from impl.nn.base.simple_loss.simple_loss_cluster_nn_v02 import SimpleLossClusterNN_V02

class ClusterNNTry00_V08(SimpleLossClusterNN_V02):
    def __init__(self, data_provider, input_count, embedding_nn=None, lstm_units=64, output_dense_units=512,
                 cluster_count_dense_layers=1, lstm_layers=3, pre_lstm_layers=3, output_dense_layers=1, cluster_count_dense_units=512,
                 weighted_classes=False):
        super().__init__(data_provider, input_count, embedding_nn, weighted_classes)

        # Network parameters
        self.__lstm_layers = lstm_layers
        self.__pre_lstm_layers = pre_lstm_layers
        self.__lstm_units = lstm_units
        self.__output_dense_units = output_dense_units
        self.__cluster_count_dense_layers = cluster_count_dense_layers
        self.__cluster_count_dense_units = cluster_count_dense_units
        self.__output_dense_layers = output_dense_layers

    def _build_network(self, network_input, network_output, additional_network_outputs):
        cluster_counts = list(self.data_provider.get_cluster_counts())

        # The simple loss cluster NN requires a specific output: a list of softmax distributions
        # First in this list are all softmax distributions for k=k_min for each object, then for k=k_min+1 for each
        # object etc. At the end, there is the cluster count output.

        # First we get an embedding for the network inputs
        embeddings = self._get_embedding(network_input)

        # Reshape all embeddings to 1d vectors
        # embedding_shape = self._embedding_nn.model.layers[-1].output_shape
        # embedding_size = np.prod(embedding_shape[1:])
        embedding_shape = embeddings[0].shape
        embedding_size = int(str(np.prod(embedding_shape[1:])))
        embedding_reshaper = self._s_layer('embedding_reshape', lambda name: Reshape((1, embedding_size), name=name))
        embeddings_reshaped = [embedding_reshaper(embedding) for embedding in embeddings]

        # Merge all embeddings to one tensor
        embeddings_merged = self._s_layer('embeddings_merge', lambda name: Concatenate(axis=1, name=name))(embeddings_reshaped)

        self._add_additional_prediction_output(embeddings_merged, '0_Embeddings')

        # The value range of the embeddings must be [-1, 1] to allow an efficient execution of the cluster evaluations,
        # so be sure the embeddings are processed by tanh or some similar activation

        # Use some LSTMs for some kind of preprocessing. After these layers a regularisation is applied
        processed = embeddings_merged
        for i in range(self.__pre_lstm_layers):
            processed = self._s_layer(
                'PRE_LSTM_proc_{}'.format(i), lambda name: Bidirectional(LSTM(self.__lstm_units, return_sequences=True), name=name)
            )(processed)
            processed = self._s_layer(
                'PRE_LSTM_proc_{}_batch'.format(i), lambda name: BatchNormalization(name=name)
            )(processed)
        # Reshape the data now to a embeddings sized representation representation
        processed = TimeDistributed(Dense(embedding_size, activation='tanh'))(processed)

        # Store these processed embeddings
        preprocessed_embeddings = [slice_layer(processed, i) for i in range(len(network_input))]
        self._add_additional_prediction_output(processed, "1_LSTM_Preprocessed_Embeddings")

        # Use now some LSTM-layer to process all embeddings
        # processed = embeddings_merged
        for i in range(self.__lstm_layers):
            processed = self._s_layer(
                'LSTM_proc_{}'.format(i), lambda name: Bidirectional(LSTM(self.__lstm_units, return_sequences=True), name=name)
            )(processed)
            processed = self._s_layer(
                'LSTM_proc_{}_batch'.format(i), lambda name: BatchNormalization(name=name)
            )(processed)

        # Split the tensor to seperate layers
        embeddings_processed = [self._s_layer('slice_{}'.format(i), lambda name: slice_layer(processed, i, name)) for i in range(len(network_input))]

        # Create now two outputs: The cluster count and for each cluster count / object combination a softmax distribution.
        # These outputs are independent of each other, therefore it doesn't matter which is calculated first. Let us start
        # with the cluster count / object combinations.

        # First prepare some generally required layers
        layers = []
        for i in range(self.__output_dense_layers):
            layers += [
                self._s_layer('output_dense{}'.format(i), lambda name: Dense(self.__output_dense_units, name=name)),
                self._s_layer('output_batch'.format(i), lambda name: BatchNormalization(name=name)),
                # self._s_layer('output_relu'.format(i), lambda name: Activation('relu', name=name))
                LeakyReLU()
            ]
        cluster_softmax = {
            k: self._s_layer('softmax_cluster_{}'.format(k), lambda name: Dense(k, activation='softmax', name=name)) for k in cluster_counts
        }

        # Create now the outputs
        clusters_output = additional_network_outputs['clusters'] = {}
        cluster_classifiers = {k: [] for k in cluster_counts}
        for i in range(len(embeddings_processed)):
            embedding_proc = embeddings_processed[i]

            # Add the required layers
            for layer in layers:
                embedding_proc = layer(embedding_proc)

            input_clusters_output = clusters_output['input{}'.format(i)] = {}
            for k in cluster_counts:

                # Create now the required softmax distributions
                output_classifier = cluster_softmax[k](embedding_proc)
                input_clusters_output['cluster{}'.format(k)] = output_classifier
                network_output.append(output_classifier)

                cluster_classifiers[k].append(output_classifier)

        clustering_quality = 0
        sum_cohesion = 0
        sum_separation = 0
        alpha = 0.5
        beta = 0.25
        self._add_debug_output(Concatenate(axis=1)(preprocessed_embeddings), 'eval_embeddings')

        # # Squared euclidean distance
        # distance_f = lambda x, y: K.sum(K.square(x - y), axis=2)

        # Euclidean distance
        distance_f = lambda x, y: K.sqrt(K.sum(K.square(x - y), axis=2))

        for k in cluster_counts:

            # Create evaluation metrics
            self._add_debug_output(Concatenate(axis=1)(cluster_classifiers[k]), 'eval_classifications_k{}'.format(k))
            cluster_centers = get_cluster_centers(preprocessed_embeddings, cluster_classifiers[k])
            self._add_debug_output(Concatenate(axis=1)(cluster_centers), 'eval_cluster_centers_k{}'.format(k))
            cohesion = get_cluster_cohesion(cluster_centers, preprocessed_embeddings, cluster_classifiers[k])
            self._add_debug_output(cohesion, 'eval_cohesion_k{}'.format(k))
            separation = get_cluster_separation(cluster_centers, cluster_classifiers[k])
            self._add_debug_output(separation, 'eval_separation_k{}'.format(k))

            self._add_additional_prediction_output(
                Concatenate(axis=1, name='2_cluster_centers_k{}'.format(k))(cluster_centers),
                'cluster_centers_k{}'.format(k)
            )

            sum_cohesion = Lambda(lambda cohesion: sum_cohesion + cohesion)(cohesion)
            sum_separation = Lambda(lambda separation: sum_separation + separation)(separation)
            # clustering_quality = Lambda(lambda cohesion:
            #
            #     # Update the loss
            #     clustering_quality + (alpha * cohesion - beta * separation)
            # )(cohesion)

        # Add alpha and beta to the cohesion and the separation
        sum_cohesion = Lambda(lambda sum_cohesion: alpha * sum_cohesion)(sum_cohesion)
        sum_separation = Lambda(lambda sum_separation: beta * sum_separation)(sum_separation)

        # Normalize the cohesion and the separation by the cluster_counts
        sum_cohesion = Lambda(lambda sum_cohesion: sum_cohesion / len(cluster_counts))(sum_cohesion)
        sum_separation = Lambda(lambda sum_separation: sum_separation / len(cluster_counts))(sum_separation)

        # Add the losses for the cohesion and the separation. Use for both the same loss function
        cluster_quality_loss = lambda x: lambda similiarty_loss, x=x: K.exp(- similiarty_loss * similiarty_loss * 4) * x
        self._register_additional_grouping_similarity_loss(
            'cluster_cohesion',
            cluster_quality_loss(sum_cohesion)
        )
        self._register_additional_grouping_similarity_loss(
            'cluster_separation',
            cluster_quality_loss(- K.log(1 + 2 * sum_separation))
        )

        # # Normalize the cluster quality
        # clustering_quality = Lambda(lambda x: x / len(cluster_counts))(clustering_quality)
        #
        # # What to do with the cluster quality? We use it for an additional loss, this loss should optimize
        # # the cluster quality as soon as the clustering works relatively well.
        # self._register_additional_grouping_similarity_loss(
        #     'cluster_quality',
        #     lambda similiarty_loss: K.exp(- similiarty_loss * similiarty_loss) * clustering_quality
        # )

        # Calculate the real cluster count
        cluster_count = self._s_layer('cluster_count_LSTM_merge', lambda name: Bidirectional(LSTM(self.__lstm_units), name=name)(embeddings_merged))
        cluster_count = self._s_layer('cluster_count_LSTM_merge_batch', lambda name: BatchNormalization(name=name))(cluster_count)
        for i in range(self.__cluster_count_dense_layers):
            cluster_count = self._s_layer('cluster_count_dense{}'.format(i), lambda name: Dense(self.__cluster_count_dense_units, name=name))(cluster_count)
            cluster_count = self._s_layer('cluster_count_batch{}'.format(i), lambda name: BatchNormalization(name=name))(cluster_count)
            # cluster_count = self._s_layer('cluster_count_relu{}'.format(i), lambda name: Activation('relu', name=name))(cluster_count)
            cluster_count = LeakyReLU()(cluster_count)

        # The next layer is an output-layer, therefore the name must not be formatted
        cluster_count = self._s_layer(
            'cluster_count_output',
            lambda name: Dense(len(cluster_counts), activation='softmax', name=name),
            format_name=False
        )(cluster_count)
        additional_network_outputs['cluster_count_output'] = cluster_count

        network_output.append(cluster_count)

        return True