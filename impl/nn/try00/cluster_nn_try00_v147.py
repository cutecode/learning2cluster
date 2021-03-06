from keras.layers import Reshape, Concatenate, Bidirectional, Dense, BatchNormalization, \
    Activation, Lambda, Add, CuDNNLSTM, Dropout
from keras.layers.advanced_activations import LeakyReLU
from keras.losses import mean_squared_error

import numpy as np

from core.nn.helper import slice_layer, lukic_kl_divergence, reweight_values, concat, ExtendedDropout, regularizer_cluster_assignment_new
from impl.nn.base.simple_loss.simple_loss_cluster_nn_v02 import SimpleLossClusterNN_V02

# from keras.layers import LSTM
LSTM = CuDNNLSTM

class ClusterNNTry00_V147(SimpleLossClusterNN_V02):
    def __init__(self, data_provider, input_count, embedding_nn=None, output_dense_units=512,
                 cluster_count_dense_layers=1, lstm_layers=5, output_dense_layers=1, cluster_count_dense_units=512,
                 weighted_classes=False, cluster_count_lstm_layers=2, cluster_count_lstm_units=64, internal_embedding_size=96,
                 kl_embedding_size=128, kl_divergence_factor=1.,
                 simplified_center_loss_factor=1., cluster_assignment_regularization_factor=0.5, use_v02_cluster_assignment_loss=False):
        super().__init__(data_provider, input_count, embedding_nn, weighted_classes, include_input_count_in_name=False)

        self.__internal_embedding_size = internal_embedding_size

        # Network parameters
        self.__lstm_layers = lstm_layers
        self.__output_dense_units = output_dense_units
        self.__cluster_count_lstm_layers = cluster_count_lstm_layers
        self.__cluster_count_lstm_units = cluster_count_lstm_units
        self.__cluster_count_dense_layers = cluster_count_dense_layers
        self.__cluster_count_dense_units = cluster_count_dense_units
        self.__output_dense_layers = output_dense_layers
        self.__kl_embedding_size = kl_embedding_size
        self.__kl_divergence_factor = kl_divergence_factor
        self.__simplified_center_loss_factor = simplified_center_loss_factor
        self.__cluster_assignment_regularization_factor = cluster_assignment_regularization_factor
        self.__use_v02_cluster_assignment_loss = use_v02_cluster_assignment_loss

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

        # The KL-divergence only makes sense if an embedding is used (otherwise no embedding can be optimized)
        if self._uses_embedding_layer() and self.__kl_divergence_factor > 0.:

            # Implement the KL-divergence on this layer. First, like lukic et al., do another fully connedted layer
            kl_embeddings = embeddings_reshaped
            kl_dense0 = self._s_layer('kl_dense0', lambda name: LeakyReLU(self.__kl_embedding_size, name=name))
            # kl_dense0 = self._s_layer('kl_dense0', lambda name: Activation(self.__kl_embedding_size, name=name, activation='relu'))
            kl_embeddings = [kl_dense0(kl_embedding) for kl_embedding in kl_embeddings]
            kl_softmax = self._s_layer('kl_softmax', lambda name: Dense(self.__kl_embedding_size, name=name, activation='softmax'))
            kl_embeddings = [kl_softmax(kl_embedding) for kl_embedding in kl_embeddings]
            self._register_additional_embedding_comparison_regularisation(
                'KL_divergence',
                lukic_kl_divergence,
                kl_embeddings,
                weight=self.__kl_divergence_factor
            )

        # We need now the internal representation of the embeddings. This means we have to resize them.
        internal_embedding_size = self.__internal_embedding_size // 2 * 2
        embedding_internal_resizer = self._s_layer('internal_embedding_resize', lambda name: Dense(internal_embedding_size, name=name))
        embeddings_reshaped = [embedding_internal_resizer(embedding) for embedding in embeddings_reshaped]
        embedding_internal_resizer_act = LeakyReLU()
        embeddings_reshaped = [embedding_internal_resizer_act(embedding) for embedding in embeddings_reshaped]

        # Merge all embeddings to one tensor
        embeddings_merged = self._s_layer('embeddings_merge', lambda name: Concatenate(axis=1, name=name))(embeddings_reshaped)

        # Use now some lstm-layers
        processed = embeddings_merged
        second_last_processed = None
        for i in range(self.__lstm_layers):
            second_last_processed = processed
            tmp = self._s_layer(
                'LSTM_proc_{}'.format(i), lambda name: Bidirectional(LSTM(internal_embedding_size // 2, return_sequences=True), name=name)
            )(processed)
            processed = Add()([processed, tmp])

        # Split the tensor to seperate layers
        embeddings_processed = [self._s_layer('slice_{}'.format(i), lambda name: slice_layer(processed, i, name)) for i in range(len(network_input))]

        # Implement the simplified center loss
        center_loss_vectors = embeddings_processed
        def simple_center_loss(y_true, y_pred):

            # y_true has no fixed shape, but we require that the second dimension size is already known. Reshape it.
            n = self.input_count
            if self.include_self_comparison:
                y_len = n * (n + 1) // 2
            else:
                y_len = n * (n - 1) // 2
            y_true = Lambda(lambda x: y_true)(center_loss_vectors[0])
            y_true = Reshape((y_len,))(y_true)

            # # Dummy values
            # y_true = Lambda(lambda y_true: 0. * y_true)(y_true)

            # Calculate the centers for each vector and then the loss (just MSE)
            centers = reweight_values(center_loss_vectors, y_true)
            return mean_squared_error(
                concat(centers, axis=1),
                concat(center_loss_vectors, axis=1)
            )
        if self.__simplified_center_loss_factor > 0.:
            self._register_additional_grouping_similarity_loss(
                'simple_center_loss', simple_center_loss, False, weight=self.__simplified_center_loss_factor
            )

        # Create now two outputs: The cluster count and for each cluster count / object combination a softmax distribution.
        # These outputs are independent of each other, therefore it doesn't matter which is calculated first. Let us start
        # with the cluster count / object combinations.

        # First prepare some generally required layers
        layers = []
        for i in range(self.__output_dense_layers):
            layers += [
                self._s_layer('output_dense{}'.format(i), lambda name: Dense(self.__output_dense_units, name=name)),
                self._s_layer('output_batch'.format(i), lambda name: BatchNormalization(name=name)),
                LeakyReLU(),
                self._s_layer('output_relu'.format(i), lambda name: Activation(LeakyReLU(), name=name)),
                Dropout(0.5)
            ]

            # Add forward pass dropout
            if self._try_get_additional_build_config_value('forward_pass_dropout', default_value=False):
                layers.append(ExtendedDropout(0.5, train_phase_active=False, test_phase_active=True))
        cluster_softmax = {
            k: self._s_layer('softmax_cluster_{}'.format(k), lambda name: Dense(k, activation='softmax', name=name)) for k in cluster_counts
        }

        # Prepare a data structure for the cluster assignement regularizer ("implication rule")
        softmax_outputs = {k:[] for k in cluster_counts}

        # Create now the outputs
        clusters_output = additional_network_outputs['clusters'] = {}
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

                # Add the output for the regularizer
                softmax_outputs[k].append(output_classifier)

        cluster_assignment_regularization = regularizer_cluster_assignment_new(softmax_outputs, use_v02_loss=self.__use_v02_cluster_assignment_loss)
        self._register_additional_regularisation(
            cluster_assignment_regularization,
            'cluster_assignment_regularization',
            weight=self.__cluster_assignment_regularization_factor
        )

        # Calculate the real cluster count
        assert self.__cluster_count_lstm_layers >= 1
        cluster_count = second_last_processed
        for i in range(self.__cluster_count_lstm_layers - 1):
            cluster_count = self._s_layer('cluster_count_LSTM{}'.format(i), lambda name: Bidirectional(LSTM(self.__cluster_count_lstm_units, return_sequences=True), name=name)(cluster_count))
            cluster_count = self._s_layer('cluster_count_LSTM{}_batch'.format(i), lambda name: BatchNormalization(name=name))(cluster_count)
        cluster_count = self._s_layer('cluster_count_LSTM_merge', lambda name: Bidirectional(LSTM(self.__cluster_count_lstm_units), name=name)(cluster_count))
        cluster_count = self._s_layer('cluster_count_LSTM_merge_batch', lambda name: BatchNormalization(name=name))(cluster_count)
        for i in range(self.__cluster_count_dense_layers):
            cluster_count = self._s_layer('cluster_count_dense{}'.format(i), lambda name: Dense(self.__cluster_count_dense_units, name=name))(cluster_count)
            cluster_count = LeakyReLU()(cluster_count)
            cluster_count = self._s_layer('cluster_count_batch{}'.format(i), lambda name: BatchNormalization(name=name))(cluster_count)
            cluster_count = Dropout(0.5)(cluster_count)

            # Add forward pass dropout
            if self._try_get_additional_build_config_value('forward_pass_dropout', default_value=False):
                cluster_count = ExtendedDropout(0.5, train_phase_active=False, test_phase_active=True)(cluster_count)

            # cluster_count = self._s_layer('cluster_count_relu{}'.format(i), lambda name: Activation(LeakyReLU(), name=name))(cluster_count)

        # The next layer is an output-layer, therefore the name must not be formatted
        cluster_count = self._s_layer(
            'cluster_count_output',
            lambda name: Dense(len(cluster_counts), activation='softmax', name=name),
            format_name=False
        )(cluster_count)
        additional_network_outputs['cluster_count_output'] = cluster_count

        network_output.append(cluster_count)

        return True