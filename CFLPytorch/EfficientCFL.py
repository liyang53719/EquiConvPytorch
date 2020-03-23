import torch
from torch import nn
from torch.nn import functional as F

from .utils import (
    round_filters,
    round_repeats,
    drop_connect,
    get_same_padding_conv2d,
    get_model_params,
    efficientnet_params,
    load_pretrained_weights,
    Swish,
    MemoryEfficientSwish,
)

class MBConvBlock(nn.Module):
    """
    Mobile Inverted Residual Bottleneck Block

    Args:
        block_args (namedtuple): BlockArgs, see above
        global_params (namedtuple): GlobalParam, see above

    Attributes:
        has_se (bool): Whether the block contains a Squeeze and Excitation layer.
    """

    def __init__(self, block_args, global_params, conv_type):
        super().__init__()
        self._block_args = block_args
        self._bn_mom = 1 - global_params.batch_norm_momentum
        self._bn_eps = global_params.batch_norm_epsilon
        self.has_se = (self._block_args.se_ratio is not None) and (0 < self._block_args.se_ratio <= 1)
        self.id_skip = block_args.id_skip  # skip connection and drop connect
        self._conv_type = conv_type

        # Get static or dynamic convolution depending on image size
        Conv2d = get_same_padding_conv2d(image_size=global_params.image_size, conv_type= self._conv_type)

        # Expansion phase
        inp = self._block_args.input_filters  # number of input channels
        oup = self._block_args.input_filters * self._block_args.expand_ratio  # number of output channels
        if self._block_args.expand_ratio != 1:
            self._expand_conv = Conv2d(in_channels=inp, out_channels=oup, kernel_size=1, bias=False)
            self._bn0 = nn.BatchNorm2d(num_features=oup, momentum=self._bn_mom, eps=self._bn_eps)

        # Depthwise convolution phase
        k = self._block_args.kernel_size
        s = self._block_args.stride
        self._depthwise_conv = Conv2d(
            in_channels=oup, out_channels=oup, groups=oup,  # groups makes it depthwise
            kernel_size=k, stride=s, bias=False)
        self._bn1 = nn.BatchNorm2d(num_features=oup, momentum=self._bn_mom, eps=self._bn_eps)

        # Squeeze and Excitation layer, if desired
        if self.has_se:
            num_squeezed_channels = max(1, int(self._block_args.input_filters * self._block_args.se_ratio))
            self._se_reduce = Conv2d(in_channels=oup, out_channels=num_squeezed_channels, kernel_size=1)
            self._se_expand = Conv2d(in_channels=num_squeezed_channels, out_channels=oup, kernel_size=1)

        # Output phase
        final_oup = self._block_args.output_filters
        self._project_conv = Conv2d(in_channels=oup, out_channels=final_oup, kernel_size=1, bias=False)
        self._bn2 = nn.BatchNorm2d(num_features=final_oup, momentum=self._bn_mom, eps=self._bn_eps)
        self._swish = MemoryEfficientSwish()

    def forward(self, inputs, drop_connect_rate=None):
        """
        :param inputs: input tensor
        :param drop_connect_rate: drop connect rate (float, between 0 and 1)
        :return: output of block
        """

        # Expansion and Depthwise Convolution
        x = inputs
        if self._block_args.expand_ratio != 1:
            x = self._swish(self._bn0(self._expand_conv(inputs)))
        x = self._swish(self._bn1(self._depthwise_conv(x)))

        # Squeeze and Excitation
        if self.has_se:
            x_squeezed = F.adaptive_avg_pool2d(x, 1)
            x_squeezed = self._se_expand(self._swish(self._se_reduce(x_squeezed)))
            x = torch.sigmoid(x_squeezed) * x

        x = self._bn2(self._project_conv(x))

        # Skip connection and drop connect
        input_filters, output_filters = self._block_args.input_filters, self._block_args.output_filters
        if self.id_skip and self._block_args.stride == 1 and input_filters == output_filters:
            if drop_connect_rate:
                x = drop_connect(x, p=drop_connect_rate, training=self.training)
            x = x + inputs  # skip connection
        return x

    def set_swish(self, memory_efficient=True):
        """Sets swish function as memory efficient (for training) or standard (for export)"""
        self._swish = MemoryEfficientSwish() if memory_efficient else Swish()


class EfficientNet(nn.Module):
    """
    An EfficientNet model. Most easily loaded with the .from_name or .from_pretrained methods

    Args:
        blocks_args (list): A list of BlockArgs to construct blocks
        global_params (namedtuple): A set of GlobalParams shared between blocks

    Example:
        model = EfficientNet.from_pretrained('efficientnet-b0')

    """

    def __init__(self, blocks_args=None, global_params=None, conv_type=None):
        super().__init__()
        assert isinstance(blocks_args, list), 'blocks_args should be a list'
        assert len(blocks_args) > 0, 'block args must be greater than 0'
        self._global_params = global_params
        self._blocks_args = blocks_args
        self._conv_type = conv_type

        # Get static or dynamic convolution depending on image size
        Conv2d = get_same_padding_conv2d(image_size=global_params.image_size,conv_type=self._conv_type)

        # Batch norm parameters
        bn_mom = 1 - self._global_params.batch_norm_momentum
        bn_eps = self._global_params.batch_norm_epsilon

        # Stem
        in_channels = 3  # rgb
        out_channels = round_filters(32, self._global_params)  # number of output channels
        self._conv_stem = Conv2d(in_channels, out_channels, kernel_size=3, stride=2, bias=False)
        self._bn0 = nn.BatchNorm2d(num_features=out_channels, momentum=bn_mom, eps=bn_eps)

        # Build blocks
        self._blocks = nn.ModuleList([])
        for block_args in self._blocks_args:

            # Update block input and output filters based on depth multiplier.
            block_args = block_args._replace(
                input_filters=round_filters(block_args.input_filters, self._global_params),
                output_filters=round_filters(block_args.output_filters, self._global_params),
                num_repeat=round_repeats(block_args.num_repeat, self._global_params)
            )

            # The first block needs to take care of stride and filter size increase.
            self._blocks.append(MBConvBlock(block_args, self._global_params, self._conv_type))
            if block_args.num_repeat > 1:
                block_args = block_args._replace(input_filters=block_args.output_filters, stride=1)
            for _ in range(block_args.num_repeat - 1):
                self._blocks.append(MBConvBlock(block_args, self._global_params, self._conv_type))

        # Head
        in_channels = block_args.output_filters  # output of final block
        out_channels = round_filters(1280, self._global_params)
        self._conv_head = Conv2d(in_channels, out_channels, kernel_size=1, bias=False)
        self._bn1 = nn.BatchNorm2d(num_features=out_channels, momentum=bn_mom, eps=bn_eps)

        # Final linear layer
        self._avg_pooling = nn.AdaptiveAvgPool2d(1)
        self._dropout = nn.Dropout(self._global_params.dropout_rate)
        self._fc = nn.Linear(out_channels, self._global_params.num_classes)
        self._swish = MemoryEfficientSwish()

    def set_swish(self, memory_efficient=True):
        """Sets swish function as memory efficient (for training) or standard (for export)"""
        self._swish = MemoryEfficientSwish() if memory_efficient else Swish()
        for block in self._blocks:
            block.set_swish(memory_efficient)


    def extract_features(self, inputs):
        """ Returns output of the final convolution layer """
        skipconnection={}
        # Stem
        x = self._swish(self._bn0(self._conv_stem(inputs)))
        skipconnection[0] = x

        # Blocks
        index = 0
        for idx, block in enumerate(self._blocks):
            drop_connect_rate = self._global_params.drop_connect_rate
            if drop_connect_rate:
                drop_connect_rate *= float(idx) / len(self._blocks)
            x = block(x, drop_connect_rate=drop_connect_rate)
            skipconnection[idx+1] = x
            index+= 1

        # Head
        x = self._swish(self._bn1(self._conv_head(x)))
     
     #------------------------------------------------------------------------------------  
        # decoder EDGE MAPS & CORNERS MAPS   

        Conv2d = get_same_padding_conv2d(image_size=self._global_params.image_size, conv_type=self._conv_type)
        conv1a = Conv2d(x.shape[1], 512, kernel_size=3, bias=True, stride=1)
        d_2x_ec = self._swish(conv1a(x))
        d_2x = F.interpolate(d_2x_ec, scale_factor=2, mode="bilinear", align_corners=True)

        #for i in range(index):
        #    print("index: ",index-i, "shape: ", skipconnection[index-i].shape[1])

        d_concat_2x = torch.cat((d_2x,skipconnection[index-5]),dim=1)
        conv1b = Conv2d(d_concat_2x.shape[1], 256, kernel_size=3, bias=True, stride=1)
        d_4x_ec = self._swish(conv1b(d_concat_2x))
        d_4x = F.interpolate(d_4x_ec, scale_factor=2, mode="bilinear", align_corners=True)
        conv1c = Conv2d(d_4x.shape[1], 2, kernel_size=3, bias=True, stride=1)
        output4x_likelihood = conv1c(d_4x)

        d_concat_4x = torch.cat((d_4x,skipconnection[index-11],output4x_likelihood),dim=1)
        conv2a = Conv2d(d_concat_4x.shape[1], 128, kernel_size=3, bias=True, stride=1)
        d_8x_ec = self._swish(conv2a(d_concat_4x))
        d_8x = F.interpolate(d_8x_ec, scale_factor=2, mode="bilinear", align_corners=True)
        conv2b = Conv2d(d_8x.shape[1], 2, kernel_size=3, bias=True, stride=1)
        output8x_likelihood = conv2b(d_8x)

        d_concat_8x = torch.cat((d_8x,skipconnection[index-13],output8x_likelihood),dim=1)
        conv3a = Conv2d(d_concat_8x.shape[1], 64, kernel_size=5, bias=True, stride=1)
        d_16x_ec = self._swish(conv3a(d_concat_8x))
        d_16x = F.interpolate(d_16x_ec, scale_factor=2, mode="bilinear", align_corners=True)
        conv3b = Conv2d(d_16x.shape[1], 2, kernel_size=3, bias=True, stride=1)
        output16x_likelihood = conv3b(d_16x)

        d_concat_16x = torch.cat((d_16x,skipconnection[index-15],output16x_likelihood),dim=1)
        conv4a = Conv2d(d_concat_16x.shape[1], 64, kernel_size=5, bias=True, stride=1)
        d_16x_conv1 = self._swish(conv4a(d_concat_16x))
        conv4b = Conv2d(d_16x_conv1.shape[1], 2, kernel_size=3, bias=True, stride=1)
        output_likelihood = conv4b(d_16x_conv1)
        
        
        return output_likelihood

    def forward(self, inputs):
        """ Calls extract_features to extract features, applies final linear layer, and returns logits. """
        bs = inputs.size(0)
        # Convolution layers
        x = self.extract_features(inputs)
        """
        # Pooling and final linear layer
        x = self._avg_pooling(x)
        x = x.view(bs, -1)
        x = self._dropout(x)
        x = self._fc(x)
        """
        return x
        

    @classmethod
    def from_name(cls, model_name, conv_type, override_params=None):
        cls._check_model_name_is_valid(model_name)
        blocks_args, global_params = get_model_params(model_name, override_params)
        return cls(blocks_args, global_params, conv_type)

    @classmethod
    def from_pretrained(cls, model_name, conv_type, advprop=False, num_classes=1000, in_channels=3):
        model = cls.from_name(model_name, conv_type, override_params={'num_classes': num_classes})
        load_pretrained_weights(model, model_name, load_fc=(num_classes == 1000), advprop=advprop)
        if in_channels != 3:
            Conv2d = get_same_padding_conv2d(image_size = model._global_params.image_size, conv_type=model._conv_type)
            out_channels = round_filters(32, model._global_params)
            model._conv_stem = Conv2d(in_channels, out_channels, kernel_size=3, stride=2, bias=False)
        return model
    
    @classmethod
    def get_image_size(cls, model_name):
        cls._check_model_name_is_valid(model_name)
        _, _, res, _ = efficientnet_params(model_name)
        return res

    @classmethod
    def _check_model_name_is_valid(cls, model_name):
        """ Validates model name. """ 
        valid_models = ['efficientnet-b'+str(i) for i in range(9)]
        if model_name not in valid_models:
            raise ValueError('model_name should be one of: ' + ', '.join(valid_models))

"""
if __name__ == '__main__':
    input0 = torch.randn(1,3,224,224)
    model = EfficientNet.from_name('efficientnet-b0','Equi')
    output0 = model(input0)
    print(output0.shape)
"""    