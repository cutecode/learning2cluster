import matplotlib
matplotlib.use('Agg')

import numpy as np
import random

from keras.layers.advanced_activations import LeakyReLU
from keras.optimizers import Adadelta

from random import randint
from time import time

from impl.nn.try00.cluster_nn_try00_v98 import ClusterNNTry00_V98

from core.nn.misc.cluster_count_uncertainity import measure_cluster_count_uncertainity
from core.nn.misc.hierarchical_clustering import hierarchical_clustering

if __name__ == '__main__':

    # Difference to test_cluster_nn_try00.py: No embedding is used and the network always returns that 10 clusters were
    # found, but some of them may be empty

    from sys import platform

    from impl.data.audio.timit_data_provider import TIMITDataProvider
    from impl.nn.base.embedding_nn.cnn_embedding import CnnEmbedding

    is_linux = platform == "linux" or platform == "linux2"
    top_dir = "/tmp/" if is_linux else "G:/tmp/"
    ds_dir = "./" if is_linux else "../"

    TIMIT_lst = TIMITDataProvider.load_speaker_list(ds_dir + 'datasets/TIMIT/traininglist_100/testlist_200.txt')
    dp = TIMITDataProvider(
        # data_dir=top_dir + "/test/TIMIT_mini", cache_directory=top_dir + "/test/cache",
        data_dir=top_dir + "/test/TIMIT", cache_directory=top_dir + "/test/cache",
        min_cluster_count=1,
        max_cluster_count=5,
        return_1d_audio_data=False,
        test_classes=TIMIT_lst,
        validate_classes=TIMIT_lst,
        concat_audio_files_of_speaker=True,

        minimum_snippets_per_cluster=[(200, 200), (100, 100)],
        window_width=[(100, 200)]
    )
    en = CnnEmbedding(
        output_size=256, cnn_layers_per_block=1, block_feature_counts=[32, 64, 128],
        fc_layer_feature_counts=[256], hidden_activation=LeakyReLU(), final_activation=LeakyReLU(),
        batch_norm_for_init_layer=False, batch_norm_after_activation=True, batch_norm_for_final_layer=True
    )

    max_input_count = 20
    def get_c_nn(input_count=max_input_count):
        c_nn = ClusterNNTry00_V98(dp, input_count, en, lstm_layers=7, internal_embedding_size=96, cluster_count_dense_layers=1, cluster_count_dense_units=512,
                              output_dense_layers=0, output_dense_units=256, cluster_count_lstm_layers=1, cluster_count_lstm_units=128,
                              kl_embedding_size=128, kl_divergence_factor=0.1, simplified_center_loss_factor=0.1)
        c_nn.include_self_comparison = False
        c_nn.weighted_classes = True
        c_nn.class_weights_approximation = 'stochastic'
        c_nn.minibatch_size = 15
        c_nn.class_weights_post_processing_f = lambda x: np.sqrt(x)
        c_nn.set_loss_weight('similarities_output', 5.0)
        c_nn.optimizer = Adadelta(lr=5.0)

        validation_factor = 10
        c_nn.early_stopping_iterations = 15001
        c_nn.validate_every_nth_epoch = 10 * validation_factor
        c_nn.validation_data_count = c_nn.minibatch_size * validation_factor
        # c_nn.prepend_base_name_to_layer_name = False
        return c_nn

    # Create all possible input counts from 2*max_cluster_size (every cluster contains at least 2 records) up to the maximal input count
    # input_counts = list(range(1 + dp.get_target_max_cluster_count() * 2, 1 + 20))
    input_counts = [max_input_count]

    # Create an array with all possible models
    models = []

    master_network = None
    print_loss_plot_every_nth_itr = 100
    autosave_dir = top_dir + 'test/autosave_ClusterNNTry00_V98' # Autosave directory
    for input_count in reversed(sorted(input_counts)):
        print("Initialize model with {} inputs...".format(input_count))

        cnn = get_c_nn(input_count)
        models.append(cnn)

        # Share the state between all networks. This includes the history etc.
        # This must be done before the target network is built.
        if master_network is None:
            master_network = cnn
        else:
            cnn.share_state(master_network)

        # Build the network
        cnn.build_networks(print_summaries=False, build_training_model=True)

        # Register autosave, try to load the latest weights and then start / continue the training
        cnn.register_autosave(autosave_dir, example_count=10, nth_iteration=500, train_examples_nth_iteration=2000,
                               print_loss_plot_every_nth_itr=print_loss_plot_every_nth_itr)

        # Just the master network has to load all weights
        if master_network == cnn:
            cnn.try_load_from_autosave(autosave_dir)

    # Train a loooong time
    # cnn.train(1000000)

    # Train until Early Stopping kills the process
    for i in range(1000000):

        if master_network._do_validation():
            nn = master_network # Choose the model with the most input counts (=the master model)
        else:
            nn = random.choice(models)

        # Stop is early stopping was used
        if nn.train(1):
            break

    # Load the best weights and create some examples
    master_network.try_load_from_autosave(autosave_dir, config='best')
    master_network.test_network(count=30, output_directory=autosave_dir + '/examples_final', data_type='test', create_date_dir=False)

    #########################################################################################
    # Do some advanced tests: Use forward dropout to measure how "confident" the network is.
    #########################################################################################
    confident_test_n = 5 # the number of such confidence tests

    output_dir = autosave_dir + '/measure_cluster_count_uncertainity'
    tests = []
    # Build the network with the forward pass dropout
    c_nn = get_c_nn()
    c_nn.build_networks(build_training_model=False, additional_build_config={
        'forward_pass_dropout': True
    })
    c_nn.try_load_from_autosave(autosave_dir, config='best')
    for i in range(confident_test_n):
        current_output_dir = output_dir + '/test{:02d}'.format(i)
        print("Current output directory: {}".format(current_output_dir))

        print("Do the forward pass dropout test")
        # 1) Create some test data records
        records = c_nn.input_count
        fd_data, fd_additional_obj_info, fd_hints = c_nn.data_provider.get_data(elements_per_cluster_collection=records, data_type='test', cluster_collection_count=1)
        fd_x_data, _ = c_nn._build_Xy_data(fd_data, ignore_length=True)
        fd_i_data = c_nn.data_to_cluster_indices(fd_data)
        # Use only the first cluster collection
        fd_x_data = list(map(lambda x: x[0], fd_x_data[:-1]))
        fd_i_data = fd_i_data[0]
        # 2) Do the forward dropout test
        x = measure_cluster_count_uncertainity(c_nn, fd_x_data,
                show_progress=False, output_directory=current_output_dir,
                input_permutation=True, forward_pass_dropout=True)

        # Use hierarchical clustering to test the created embeddings

        # 1) Generate some data (e.g. 50 records)
        print("Try to use hierarchical clustering on the embeddings")
        records = 50
        data, _, _ = c_nn.data_provider.get_data(elements_per_cluster_collection=records, data_type='test', cluster_collection_count=1)
        x_data, _ = c_nn._build_Xy_data(data, ignore_length=True)
        i_data = c_nn.data_to_cluster_indices(data)
        # Only use the first cluster collection
        x_data = list(map(lambda x: x[0], x_data[:-1]))
        i_data = i_data[0]

        # 2) Do the test
        mrs, homogeneity_scores, completeness_scores, thresholds = hierarchical_clustering(
            x_data, i_data, c_nn, plot_filename=output_dir + '/{:02d}_rand_example_hierarchical_clustering.png'.format(i)
        )

        # 3) Also do the test with the forward pass dropout data
        mrs, homogeneity_scores, completeness_scores, thresholds = hierarchical_clustering(
            fd_x_data, fd_i_data, c_nn, plot_filename=current_output_dir + '/example_hierarchical_clustering.png'
        )

        tests.append({
            'directory': current_output_dir,
            'fd_data': (fd_data, fd_additional_obj_info, fd_hints)
        })

    #########################################################################################
    # Do a "normal" test with the data of the forward dropout test: This really helps to
    # get a feeling for the data.
    #########################################################################################
    c_nn = get_c_nn()
    c_nn.build_networks(build_training_model=False)
    c_nn.try_load_from_autosave(autosave_dir, config='best')
    for test in tests:
        directory = test['directory']
        c_nn.test_network(output_directory=directory, create_date_dir=False, data=test['fd_data'])
