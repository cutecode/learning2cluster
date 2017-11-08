import numpy as np
import numbers

from keras.utils import plot_model
from keras.models import Model
from keras.layers import Input, Dense, Activation, BatchNormalization, InputSpec, Layer, Lambda, RepeatVector, Reshape, \
    multiply, add, Flatten
from keras.regularizers import l2
import keras.regularizers
import keras.initializers
import keras.backend as K

# Print iterations progress
def printProgressBar (iteration, total, prefix = '', suffix = '', decimals = 1, length = 100, fill = '#'):
    """
    See: https://stackoverflow.com/a/34325723/916672

    Call in a loop to create terminal progress bar
    @params:
        iteration   - Required  : current iteration (Int)
        total       - Required  : total iterations (Int)
        prefix      - Optional  : prefix string (Str)
        suffix      - Optional  : suffix string (Str)
        decimals    - Optional  : positive number of decimals in percent complete (Int)
        length      - Optional  : character length of bar (Int)
        fill        - Optional  : bar fill character (Str)
    """
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filledLength = int(length * iteration // total)
    bar = fill * filledLength + '-' * (length - filledLength)
    print('\r%s |%s| %s%% %s' % (prefix, bar, percent, suffix), end = '\r')
    # Print New Line on Complete
    if iteration == total:
        print()

class C_L2(keras.regularizers.Regularizer):
    """Regularizer for L1 and L2 regularization.

    # Arguments
        l1: Float; L1 regularization factor.
        l2: Float; L2 regularization factor.
    """

    def __init__(self, l2=0., shift=None):
        self.l2 = l2
        self.shift = shift

    def __call__(self, x):

        # # This is hacky: We need a scalar and therefore we calculate the mean of l2
        # l2 = self.l2
        # if not isinstance(l2, numbers.Number):
        #     l2 = K.mean(l2, axis=0)
        l2 = self.l2

        # It may be the case that our l2 is a variable instead of a constant. It is assumed that
        # its value is for the complete batch the same. Therefore we calculate its mean over the first axis
        # (hacky, but it works)
        if not isinstance(l2, numbers.Number):
            l2 = K.mean(l2, axis=0)

        if self.shift is None:
            regularization = K.sum(l2 * K.square(x))
        else:
            regularization = l2 * K.sum(K.relu(K.square(x) - self.shift))
        return regularization

    def get_config(self):
        return {'l2': float(self.l2),'shift':float(self.shift)}

def GammaRegularizedBatchLayer(reg, max_free_gamma=1., **kwargs):
    if reg is not None:
        reg = C_L2(reg.l2, shift=max_free_gamma)
    return BatchNormalization(gamma_regularizer=reg, **kwargs)

def c_l2(l=0.01):
    return C_L2(l2=l)

def pseudo_step_function(x):
    # return ((x - K.epsilon()) / (K.abs(x) + K.epsilon()) + 1) / 2
    # return 0.5 * (1 + (x - 2 * K.epsilon()) / (K.abs(x - K.epsilon()) + K.epsilon()))
    return K.relu(K.sign(x))

class DeltaF(Layer):
    def __init__(self, alpha_regularizer=None, default_layer_count=1.,
                 **kwargs):
        super(DeltaF, self).__init__(**kwargs)
        self.supports_masking = True
        if default_layer_count < K.epsilon():
            print("The default layer count should never be smaller or equal to 0, otherwise it will never change (alsp depending on the alpha regularizer). It is recommended to use at least 0.5.")
        self.default_layer_count = default_layer_count
        self.alpha_initializer = keras.initializers.Constant(default_layer_count)
        self.alpha_regularizer = keras.regularizers.get(alpha_regularizer)

    def build(self, input_shape):
        param_shape = [1]
        self.alpha = self.add_weight(shape=param_shape,
                                     name='alpha',
                                     regularizer=self.alpha_regularizer,
                                     initializer=self.alpha_initializer)
        # Set input spec
        self.input_spec = InputSpec(ndim=len(input_shape))
        self.built = True

    def call(self, inputs, mask=None):
        return K.relu(1 - K.exp(inputs - self.alpha))

    def get_config(self):
        config = {
            'alpha_initializer': keras.initializers.serialize(self.alpha_initializer),
            'alpha_regularizer': keras.regularizers.serialize(self.alpha_regularizer),
            'default_layer_count': self.default_layer_count
        }
        base_config = super(DeltaF, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

class DynamicLayer:
    def __init__(self, name, f_layer, h_step=[lambda reg: Activation('relu')], w_init=1.0,
                 w_regularizer=(l2, 'auto'), f_regularizer=(l2, 1e-8), h_regularizer=None, res_net_like=True,
                 param_count_f=None, reweight_regularizer=True, w_step=None):
        # w_regularizer[1] = 'auto' oder 'f_regularizer' = auto =>
        # initial_w_range=(0, 10) => erstellt bereits 10 layer (macht das netzwerk performanter, da es nicht andauernd neu erstellt werden muss)

        if not isinstance(f_layer, list):
            f_layer = [f_layer]
        if not isinstance(h_step, list):
            h_step = [h_step]

        self.name = name
        self.f_layer = f_layer
        self.h_step = h_step
        self.w_init = w_init
        self.w_regularizer = w_regularizer
        self.f_regularizer = f_regularizer
        self.h_regularizer = h_regularizer
        self.parameter_count_per_f = None
        self._current_layers = []
        self._deltaF = None
        self._res_net_like = res_net_like
        self._regularization_weight = 1.0
        self._param_count_f = (lambda x: x) if param_count_f is None else param_count_f
        self._reweight_regularizer = reweight_regularizer
        self._dummy_layer = None
        self._w_step = w_step
        if param_count_f is not None and not reweight_regularizer:
            print("reweight_regularizer is False, therefore param_count_f will be ignored")

    def _get_regularizer_c(self):
        # w_regularizer = parameter_count_per_f * f_regularizer * c
        c = 5e-2 / (104 * 1e-5)
        return c

    def _get_regularizer(self, current, other, c_f, i=None):
        if current is None:
            return None
        if current[1] == 'auto':
            current = (current[0], other[1] * c_f)
        current = (current[0], current[1] * self._regularization_weight)

        f = current[0]
        l = current[1]

        # To make the model faster it can make sense to prebuild more layers than we actually are using.
        # But in this case we dont like to count their l2-regularization. So... We just can implement a factor
        # that is based on the delta-function. It returns 1 if the function is in the "active range" and 0
        # otherwise.
        if i is not None:
            if self._dummy_layer is None:
                raise Exception("No dummy layer is defined, but this is required if a dynamic regularization should be used.")
            else:
                l *= pseudo_step_function(self._deltaF(self._constant(i)))

        return f(l)

    def _get_w_regularization(self):
        c_f = 1
        if self._reweight_regularizer:
            c_f = self._get_regularizer_c() * self._param_count_f(self.parameter_count_per_f)
        return self._get_regularizer(self.w_regularizer, self.f_regularizer, c_f=c_f)

    def _get_f_regularizer(self, i=None):
        c_f = 1
        if self._reweight_regularizer:
            c_f = 1/(self._get_regularizer_c() * self._param_count_f(self.parameter_count_per_f))
        return self._get_regularizer(self.f_regularizer, self.w_regularizer, c_f=c_f, i=i)

    def _constant(self, c):
        return Lambda(lambda s: s * 0 + c)(self._dummy_layer)

    def set_reweight_regularization(self, regularization_weight):
        self._regularization_weight = regularization_weight

    def get_w(self):
        weights = self._deltaF.get_weights()
        if len(weights) == 0:
            return self.w_init
        return weights[0][0]

    def is_rebuild_required(self):
        return self.get_w() > len(self._current_layers)

    def calculate_used_parameters(self):
        return max(0, int(np.ceil(self.get_w()))) * self.parameter_count_per_f
        # return int(np.ceil(self.get_w())) * self.parameter_count_per_f

    def calculate_unused_parameters(self):
        return len(self._current_layers) * self.parameter_count_per_f - self.calculate_used_parameters()

    def build_network(self, nw):
        layer_count = int(np.ceil(self.get_w()))

        if self._w_step is not None:
            # Use w_step to define the amount of layers to build
            layer_count = int(np.ceil((layer_count / self._w_step))) * self._w_step

        dim = list(map(lambda x: int(str(x)), nw._keras_shape[1:]))
        dim_n = np.prod(dim)

        # We never remove any layer:)
        # # Remove layers if we have too many of them
        # self._current_layers = self._current_layers[:layer_count]

        # Add new layers if more layers are required
        for i in range(len(self._current_layers), layer_count):
            self._current_layers.append({
                'f_layer': self._get_layers('f', self.f_layer, self._get_f_regularizer(i)),
                'h_step': self._get_layers('f', self.h_step, self.h_regularizer)
            })

        # Build now the model; Call the input "x"
        x = nw
        for i in range(layer_count):

            # Execute the delta-function for the current layer (i)
            lf = self._deltaF(self._constant(i))

            # Resize it to the required dimension
            lf = RepeatVector(dim_n)(lf)
            lf = Reshape(dim)(lf)

            # Calculate the next layer
            l_next = x
            for layer in self._current_layers[i]['f_layer']:
                l_next = layer(l_next)

            # Weight the new calculation
            l_next = multiply([l_next, lf])

            # If required: Weight the old calculation
            if not self._res_net_like:
                lf_i = Lambda(lambda x: 1 - x)(lf)
                x = multiply([x, lf_i])

            # Sum up the two values:
            x = add([l_next, x])

            # Calculate h_step
            for layer in self._current_layers[i]['h_step']:
                x = layer(x)

        nw = x
        return nw

    def init(self, input_layer, dummy_layer=None):
        layers = self._get_layers('f', self.f_layer)

        # Build the "subnetwork"
        nw = input_layer
        for layer in layers:
            nw = layer(nw)

        # Calculate the parameter count
        parameters = 0
        for layer in layers:
            parameters += sum(list(map(lambda w: np.prod(w.shape), layer.get_weights())))
        self.parameter_count_per_f = parameters

        # Create the DeltaF layer
        self._deltaF = DeltaF(self._get_w_regularization(), default_layer_count=self.w_init)

        # Store the dummy layer
        if dummy_layer is not None:
            if len(dummy_layer._keras_shape) > 2:
                dummy_layer = Flatten()(dummy_layer)
            dummy_layer = Dense(1, kernel_initializer='zeros', trainable=False)(dummy_layer)
        self._dummy_layer = dummy_layer

        return nw

    def _get_layers(self, base_name, layer_builders, regularizer=None):
        return [layer_builders[i](regularizer) for i in range(len(layer_builders))]

class TDModel:
    def __init__(self):
        self._layers = []
        self._model = None
        self._dynamic_layers = []
        self._compile_kwargs = {}

    def append(self, layer):
        if isinstance(layer, list):
            for l in layer:
                self.append(l)
            return
        self._layers.append(layer)

    def __add__(self, layer):
        self.append(layer)
        return self

    def rebuild_model_if_required(self):
        if not any(map(lambda dl: dl.is_rebuild_required(), self._dynamic_layers)):
            return
        print("Rebuild model...")

        # A new key: This means we need to build a new model
        nw_input = self._layers[0]
        nw = nw_input
        for layer in self._layers[1:]:
            if isinstance(layer, DynamicLayer):
                nw = layer.build_network(nw)
            else:
                nw = layer(nw)

        # All layers are created. Compile now the model
        self._model = Model(nw_input, nw)
        self._model.compile(**self._compile_kwargs)

        unused_parameters = sum(map(lambda dl: dl.calculate_unused_parameters(), self._dynamic_layers))
        print("Model parameter count: {}".format(self._model.count_params() - unused_parameters))
        print("Additional unused parameters: {}".format(unused_parameters))

    def init(self, reweight_dynamic_layers=False, **compile_kwargs):
        assert len(self._layers) > 0
        assert isinstance(self._layers[0], Input((1,)).__class__)

        # Prepare the dynamic layers
        nw = self._layers[0]
        for layer in self._layers[1:]:
            if isinstance(layer, DynamicLayer):

                # We need to calculate the parameter count of the dynamic layer
                nw = layer.init(nw, dummy_layer=self._layers[0])

                # "Register" the dynamic layer
                self._dynamic_layers.append(layer)

            else:
                nw = layer(nw)

        if reweight_dynamic_layers:
            # Set a regularization factor for all dynamic layers
            avg_parameters_per_layer = np.mean(list(map(
                lambda l: l.parameter_count_per_f,
                self._dynamic_layers
            )))
            for layer in self._dynamic_layers:
                layer.reweight_regularization(layer.parameter_count_per_f / avg_parameters_per_layer)

        # Define the compile arguments
        self._compile_kwargs = compile_kwargs

        # Build the initial model
        self.rebuild_model_if_required()

    def get_depths(self):
        return {
            layer.name: layer.get_w() for layer in self._dynamic_layers
        }

    def print_depths(self):
        for layer in self._dynamic_layers:
            print("{}.w = {}".format(layer.name, layer.get_w()))

    def train_step(self, x, y, validation_data=None, rebuild_model_if_required=True, debug_print=True, **kwargs):
        assert self._model is not None
        batch_size = x.shape[0]
        self._model.fit(x, y, validation_data=validation_data, batch_size=batch_size, **kwargs)
        if debug_print:
            self.print_depths()
        if rebuild_model_if_required:
            self.rebuild_model_if_required()

    def train_batch(self, x, y, validation_data=None, batch_size=None, rebuild_model_if_required=True, debug_print=True, shuffle=True, **kwargs):
        if batch_size is None:
            batch_size = x.shape[0]
        records = x.shape[0]
        batches = int(np.ceil(records / batch_size))
        if debug_print:
            printProgressBar(0, batches)
        if shuffle:
            p = np.random.permutation(len(x))
            x, y = x[p], y[p]
        for i in range(batches):
            b_x = x[(i * batch_size):((i + 1) * batch_size)]
            b_y = y[(i * batch_size):((i + 1) * batch_size)]
            b_debug_print = debug_print
            b_validation_data = None
            if i == batches - 1:
                b_validation_data = validation_data
            else:
                b_debug_print = False
            self.train_step(
                b_x, b_y,
                validation_data=b_validation_data,
                rebuild_model_if_required=rebuild_model_if_required,
                debug_print=b_debug_print,
                verbose=1 if b_debug_print else 0,
                **kwargs
            )
            if debug_print:
                printProgressBar(i + 1, batches)


#################################### Model 1: FC ####################################
# Create a simple XOR model
units = 8
n_inputs = 4
n_used_inputs = 2
assert n_used_inputs <= n_inputs
r_c = 1e-3
model = TDModel()
model += Input((n_inputs,))
model += Dense(units, trainable=False)
model += DynamicLayer(
    'd0',
    f_regularizer=(l2, r_c*1e-5), #l2(r_c * 1e-5),
    w_regularizer=(l2, 'auto'), #l2(r_c * 5e-2),
    f_layer=[
        lambda reg: Dense(units, kernel_regularizer=reg),
        lambda reg: BatchNormalization()
    ], h_step=[
        lambda reg: Activation('relu')
    ])
model += DynamicLayer(
    'd1',
    f_regularizer=(l2, r_c*1e-5), #l2(r_c * 1e-5),
    w_regularizer=(l2, 'auto'), #l2(r_c * 5e-2),
    f_layer=[
        lambda reg: Dense(units, kernel_regularizer=reg),
        lambda reg: BatchNormalization()
    ], h_step=[
        lambda reg: Activation('relu')
    ])
model += Dense(1, trainable=False, activation='sigmoid')
# model.init(
#     optimizer='adadelta',
#     loss='binary_crossentropy',
#     metrics=['accuracy']
# )

# A data generator
def generate_xor_data(n):
    x = np.random.uniform(0, 1, (n, n_inputs))
    x[x>=.5] = 1.
    x[x<.5] = 0.
    y = np.sum(x[:, :n_used_inputs], axis=1) % 2
    y[y > 1] = 0.
    return x, y

n = 500
depths_hist = []
for i in range(0):
# for i in range(10000000):
    print("Iteration {}".format(i))
    x, y = generate_xor_data(n)
    # y *= 0 # use a constant output (less layers shoudl be required to produce a network)
    model.train_step(x, y)
    depths_hist.append(model.get_depths())
    print()

# TODO: Autoscaling vom l2-Loss für den w-Regularizer erstellen, basierend auf der Annahme, dass gilt w_l2=anzahl_p * p_l2 / c
# 5e-2 = 104 * 1e-5 / c
# => c = 104 * 1e-3 / 5 = 104 * 2e-4
# => c = anzahl(p) * 2e-4

# w = p * f * c
# 5e-2 = 104 * 1e-5 * c
# c = 0.05/0.00001/104
# c = 5e-2 / (104 * 1e-5)

#################################### Model 2: CNN ####################################
from keras.datasets import mnist, fashion_mnist, cifar10, cifar100
from keras.layers import Dropout, Convolution2D, MaxPool2D

(x_train, y_train), (x_test, y_test) = mnist.load_data()
# (x_train, y_train), (x_test, y_test) = cifar10.load_data()
# (x_train, y_train), (x_test, y_test) = fashion_mnist.load_data()
(x_train, y_train), (x_test, y_test) = cifar100.load_data()

# Shaping things
num_classes = np.prod(np.unique(y_train).shape)
print("num_classes={}".format(num_classes))
data_shape = x_train[0].shape
if len(data_shape) < 3:
    data_shape += (1,)
x_train = x_train.reshape(x_train.shape[0], *data_shape)
x_test = x_test.reshape(x_test.shape[0], *data_shape)
x_train = x_train.astype('float32')
x_test = x_test.astype('float32')
x_train /= 255
x_test /= 255
y_train = keras.utils.to_categorical(y_train, num_classes)
y_test = keras.utils.to_categorical(y_test, num_classes)
init_cnn_count = 30
assert init_cnn_count % data_shape[-1] == 0
init_cnn_repeat_factor = init_cnn_count // data_shape[-1]

# Build the model
assert n_used_inputs <= n_inputs
r_c = 1e-3
fc_units = 256
w_step = 5
model = TDModel()
model += Input(data_shape)
# model += Lambda(lambda nw: K.repeat_elements(nw, init_cnn_repeat_factor, axis=3))
model += Convolution2D(init_cnn_count, (3, 3), padding='same', trainable=False)
model += DynamicLayer(
    'dcnn0',
    w_regularizer=(c_l2, 1e-11),
    f_regularizer=(c_l2, 1e-2),
    reweight_regularizer=False,
    f_layer=[
        lambda reg: Convolution2D(init_cnn_count, (3, 3), padding='same', kernel_regularizer=reg),
        lambda reg: Dropout(0.25),
        # lambda reg: BatchNormalization(),
        # lambda reg: Activation('relu'),
        lambda reg: GammaRegularizedBatchLayer(reg),
    ], h_step=[
        lambda reg: Activation('relu'),
    ],
    w_step=w_step,
)
model += MaxPool2D()
# model += Lambda(lambda nw: K.repeat_elements(nw, 2, axis=3))
model += Convolution2D(init_cnn_count * 2, (3, 3), trainable=False, padding='same')
model += DynamicLayer(
    'dcnn1',
    w_regularizer=(c_l2, 1e-11),
    f_regularizer=(c_l2, 1e-2),
    reweight_regularizer=False,
    f_layer=[
        lambda reg: Convolution2D(init_cnn_count * 2, (3, 3), padding='same', kernel_regularizer=reg),
        lambda reg: Dropout(0.25),
        # lambda reg: BatchNormalization(),
        # lambda reg: Activation('relu'),
        lambda reg: GammaRegularizedBatchLayer(reg),
    ], h_step=[
        lambda reg: Activation('relu'),
    ],
    w_step=w_step,
)
model += MaxPool2D()
model += Flatten()
model += Dense(fc_units, trainable=False)
model += DynamicLayer(
    'dfc0',
    w_regularizer=(c_l2, 1e-11),
    f_regularizer=(c_l2, 1e-2),
    reweight_regularizer=False,
    f_layer=[
        lambda reg: Dense(fc_units, kernel_regularizer=reg),
        lambda reg: Dropout(0.25),
        # lambda reg: BatchNormalization(),
        # lambda reg: Activation('relu'),
        lambda reg: GammaRegularizedBatchLayer(reg),
    ], h_step=[
        lambda reg: Activation('relu'),
    ],
    w_step=w_step,
)
model += Dense(num_classes, activation='softmax', trainable=False)
model.init(
    optimizer='adadelta', # TODO: adadelta needs to store the state; that is quite tricky, I think...
    loss='categorical_crossentropy',
    metrics=['categorical_accuracy']
)
model._model.summary()
plot_model(model._model, to_file='E:\\model2.png')

# Helper function for shuffle
def unison_shuffled_copies(a, b):
    assert len(a) == len(b)
    p = np.random.permutation(len(a))
    return a[p], b[p]

n = 500
print()
for i in range(10000000):
    print("Epoch {}".format(i))

    # x, y = unison_shuffled_copies(x_train, y_train)
    # x = x[:n]
    # y = y[:n]
    #
    # if i % 100 == 0:
    #     validation_data = (x_test, y_test)
    # else:
    #     validation_data = None

    # # y *= 0 # use a constant output (less layers shoudl be required to produce a network)
    model.train_batch(
        x_train, y_train,
        validation_data=(x_test, y_test),
        batch_size=n,

        # # Only rebuild the model every 5th iteration (otherwise it is very inefficient if the algorithm reaches 1.9999 etc.)
        # rebuild_model_if_required=i % 5 == 0,
    )
    # model._model.fit(x_train, y_train, validation_data=(x_test, y_test), batch_size=500)
    # depths_hist.append(model.get_depths())
    print()

# TODO: Man kann steuern wie viele Layer pro Schritt hinzugefügt werden sollen (also immer in 5er Schritten usw).
# So kann verhindert werden, dass der Zustand vom OPtimizer immer verschmissen wird und das Training wird wohl
# schneller sein.
# Die Regularisierungen in f(x) sind eigentlich jeweils nur im letzten Layer notwendig

# MNIST:
# Epoch 16
# Train on 500 samples, validate on 10000 samples######################################################-| 99.2%
# Epoch 1/1
# 500/500 [==============================] - 1s 2ms/step - loss: 0.0721 - categorical_accuracy: 0.9940 - val_loss: 0.0814 - val_categorical_accuracy: 0.9919
# dcnn0.w = 0.8904051184654236
# dcnn1.w = 1.3498839139938354
# dfc0.w = 1.6405143737792969
#  |####################################################################################################| 100.0%
# ...
# Epoch 108
#  |#################################################################-----------------------------------| 65.0%
# Train on 500 samples, validate on 10000 samples######################################################-| 99.2%
# Epoch 1/1
# 500/500 [==============================] - 1s 2ms/step - loss: 0.0532 - categorical_accuracy: 0.9960 - val_loss: 0.0750 - val_categorical_accuracy: 0.9886
# dcnn0.w = 0.9996959567070007
# dcnn1.w = 1.9164406061172485
# dfc0.w = 2.00189208984375
#  |####################################################################################################| 100.0%
# CIFAR10:
# Epoch 368
# Train on 500 samples, validate on 10000 samples######################################################-| 99.0%
# Epoch 1/1
# 500/500 [==============================] - 1s 3ms/step - loss: 0.6143 - categorical_accuracy: 0.8400 - val_loss: 1.2339 - val_categorical_accuracy: 0.6707
# dcnn0.w = 0.5436192750930786
# dcnn1.w = 1.9263592958450317
# dfc0.w = 2.7511935234069824
#  |####################################################################################################| 100.0%
# CIFAR100:
# Epoch 1140
# Train on 500 samples, validate on 10000 samples######################################################-| 99.0%
# Epoch 1/1
# 500/500 [==============================] - 1s 3ms/step - loss: 1.9942 - categorical_accuracy: 0.5580 - val_loss: 3.6672 - val_categorical_accuracy: 0.2425
# dcnn0.w = 0.5876951217651367
# dcnn1.w = 1.9992059469223022
# dfc0.w = 4.000502586364746
#  |####################################################################################################| 100.0%

