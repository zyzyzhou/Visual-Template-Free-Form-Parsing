import torch
from torch import nn
from base import BaseModel
import torch.nn.functional as F
from torch.nn.utils.weight_norm import weight_norm
import math
import json
import numpy as np




class ncReLU(nn.Module):
    def __init__(self):
        super(ncReLU, self).__init__()
        self.r = nn.ReLU(inplace=False)
    def forward(self,input):
        return torch.cat([self.r(input), -self.r(-input)], 1)

#ResNet block based on:
 #No projection in the residual network https://link.springer.com/content/pdf/10.1007%2Fs10586-017-1389-z.pdf
class ResBlock(nn.Module):
    def __init__(self,in_ch,out_ch,dilation=1,norm='',downsample=False, dropout=None):
        super(ResBlock, self).__init__()
        layers=[]
        skipFirstReLU=False
        if in_ch!=out_ch:
            assert(out_ch==2*in_ch)
            layers.append(ncReLU())
            skipFirstReLU=True
        if downsample:
            layers.append(nn.AvgPool2d(2))
        if len(layers)>0:
            self.transform = nn.Sequential(*layers)
        else:
            self.transform = lambda x: x

        layers=[]
        if norm=='batch_norm':
            layers.append(nn.BatchNorm2d(out_ch))
        if norm=='instance_norm':
            layers.append(nn.InstanceNorm2d(out_ch))
        if norm=='group_norm':
            layers.append(nn.GroupNorm(8,out_ch))
        if not skipFirstReLU:
            layers.append(nn.ReLU(inplace=True)) 
        conv1=nn.Conv2d(out_ch, out_ch, kernel_size=3, padding=dilation, dilation=dilation)
        if norm=='weight_norm':
            layers.append(weight_norm(conv1))
        else:
            layers.append(conv1)


        if norm=='batch_norm':
            layers.append(nn.BatchNorm2d(out_ch))
        if norm=='instance_norm':
            layers.append(nn.InstanceNorm2d(out_ch))
        if norm=='group_norm':
            layers.append(nn.GroupNorm(8,out_ch))
        layers.append(nn.ReLU(inplace=True)) 
        if dropout is not None:
            if dropout==True or dropout=='normal':
                layers.append(nn.Dropout(p=0.1),inplace=True)
            elif dropout=='2d':
                layers.append(nn.Dropout2d(p=0.1),inplace=True)
        conv2=nn.Conv2d(out_ch, out_ch, kernel_size=3, padding=1)
        if norm=='weight_norm':
            layers.append(weight_norm(conv2))
        else:
            layers.append(conv2)

        self.side = nn.Sequential(*layers)

    def forward(self,x):
        x=self.transform(x)
        return x+self.side(x)

def convReLU(in_ch,out_ch,norm,dilation=1,kernel=3,dropout=None):
    conv2d = nn.Conv2d(in_ch,out_ch, kernel_size=kernel, padding=dilation*(kernel//2),dilation=dilation)
    #if i == len(cfg)-1:
    #    layers += [conv2d]
    #    break
    if norm=='batch_norm':
        layers = [conv2d, nn.BatchNorm2d(out_ch), nn.ReLU(inplace=True)]
    elif norm=='instance_norm':
        layers = [conv2d, nn.InstanceNorm2d(out_ch), nn.ReLU(inplace=True)]
    elif norm=='group_norm':
        layers = [conv2d, nn.GroupNorm(8,out_ch), nn.ReLU(inplace=True)]
    elif norm=='weight_norm':
        layers = [weight_norm(conv2d), nn.ReLU(inplace=True)]
    else:
        layers = [conv2d, nn.ReLU(inplace=True)]
    if dropout is not None:
        if dropout==True or dropout=='normal':
            layers.append(nn.Dropout(p=0.1),inplace=True)
        elif dropout=='2d':
            layers.append(nn.Dropout2d(p=0.1),inplace=True)
    return layers

def make_layers(cfg, dilation=1, norm=None, dropout=None):
    modules = []
    in_channels = [cfg[0]]
    
    layers=[]
    layerCodes=[]
    for i,v in enumerate(cfg[1:]):
        if v == 'M':
            modules.append(nn.Sequential(*layers))
            layers = [nn.MaxPool2d(kernel_size=2, stride=2)]
            layerCodes = [v]
        elif type(v)==str and v == 'ReLU':
            layerCodes.append( nn.ReLU(inplace=True) )
        elif type(v)==str and v[:2] == 'U+':
            if len(layers)>0:
                if type(layerCodes[0])==str and layerCodes[0][:2]=='U+':
                    layers[0].addConv(nn.Sequential(*layers[1:]))
                    modules.append(layers[0])
                else:
                    modules.append(nn.Sequential(*layers))
            layers = [up(in_channels[-1])]
            layerCodes = [v]

            in_channels.append(int(v[2:])+in_channels[-1])
        elif type(v)==str and v[0] == 'R':
            outCh=int(v[1:])
            layers.append(ResBlock(in_channels[-1],outCh,dilation,norm,dropout=dropout))
            layerCodes.append(v)
            in_channels.append(outCh)
        elif type(v)==str and v[0] == 'C': 
            outCh=int(v[1:])
            conv2d = nn.Conv2d(in_channels[-1], outCh, kernel_size=5, padding=2)
            #if i == len(cfg)-1:
            #    layers += [conv2d]
            #    break
            layers.append(conv2d)
            layerCodes.append(v)
            in_channels.append(outCh)
        elif type(v)==str and v[0] == 'D':
            outCh=int(v[1:]) #down sampling ResNet layer
            layers.append(ResBlock(in_channels[-1],outCh,dilation,norm,downsample=True,dropout=dropout))
            layerCodes.append(v)
            in_channels.append(outCh)
        elif type(v)==str and v[0] == 'U':
            outCh=int(v[1:]) #up sampling layer, linear
            layers.append(nn.ConvTranspose2d(in_channels[-1], outCh, kernel_size=2, stride=2, bias=False))
            layerCodes.append(v)
            in_channels.append(outCh)
        elif type(v)==str and v[0] == 'W': #dilated conv later
            outCh=int(v[1:])
            layers += convReLU(in_channels[-1],outCh,norm,dilation,dropout=dropout)
            layerCodes.append(outCh)
            in_channels.append(outCh)
        elif type(v)==str and v[0] == 'k': #conv later with custom kernel size
            div = v.find('-')
            kernel_size=int(v[1:div])
            outCh=int(v[div+1:])
            layers += convReLU(in_channels[-1],outCh,norm,kernel=kernel_size,dropout=dropout)
            layerCodes.append(outCh)
            in_channels.append(outCh)
        elif type(v)==str and v[0] == 'd': #conv later with custom dilation
            div = v.find('-')
            dilate=int(v[1:div])
            outCh=int(v[div+1:])
            layers += convReLU(in_channels[-1],outCh,norm,dilate,dropout=dropout)
            layerCodes.append(outCh)
            in_channels.append(outCh)
        elif type(v)==str and v[0] == 'B': #ResNet layer with custom dilation
            div = v.find('-')
            dilate=int(v[1:div])
            outCh=int(v[div+1:])
            layers.append(ResBlock(in_channels[-1],outCh,dilate,norm,dropout=dropout))
            layerCodes.append(v)
            in_channels.append(outCh)
        elif type(v)==str:
            print('Error reading net cfg, unknown later: '+v)
            exit(1)
        else:
            layers += convReLU(in_channels[-1],v,norm,dropout=dropout)
            layerCodes.append(v)
            in_channels.append(v)
    if len(layers)>0:
        if type(layerCodes[0])==str and layerCodes[0][:2]=='U+':
            layers[0].addConv(nn.Sequential(*layers[1:]))
            modules.append(layers[0])
        else:
            modules.append(nn.Sequential(*layers))
    return modules, in_channels[-1] #nn.Sequential(*layers)


class up(nn.Module):
    def __init__(self, in_ch, bilinear=True):
        super(up, self).__init__()
        self.outSize=in_ch
        if bilinear:
            self.up = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True)
            #self.up = nn.functional.interpolate
        else:
            self.up = nn.ConvTranspose2d(in_ch//2, in_ch//2, 2, stride=2)

    def addConv(self,conv):
        self.conv=conv

    def forward(self, x1, x2):
        x1 = self.up(x1)
        diffY = x2.size()[2] - x1.size()[2]
        diffX = x2.size()[3] - x1.size()[3]
        x1 = F.pad(x1, (diffX // 2, math.ceil(diffX / 2),
                        diffY // 2, math.ceil(diffY / 2)))
        x = torch.cat([x2, x1], dim=1)
        return self.conv(x)


class YoloBoxDetector(nn.Module): #BaseModel
    def __init__(self, config): # predCount, base_0, base_1):
        #super(YoloBoxDetector, self).__init__(config)
        super(YoloBoxDetector, self).__init__()
        self.forPairing=False
        self.config = config
        self.rotation = config['rotation'] if 'rotation' in config else True
        self.numBBTypes = config['number_of_box_types']
        self.numBBParams = 6 #conf,x-off,y-off,h-scale,w-scale,rot-off
        self.numLineParams = 5 #conf,x-off,y-off,h-scale,rot

        self.predPointCount = config['number_of_point_types'] if 'number_of_point_types' in config else 0
        self.predPixelCount = config['number_of_pixel_types'] if 'number_of_pixel_types' in config else 0
        self.predLineCount = config['number_of_line_types'] if 'number_of_line_types' in config else 0

        with open(config['anchors_file']) as f:
            self.anchors = json.loads(f.read()) #array of objects {rot,height,width}
        if self.rotation:
            self.meanH=48.0046359128/2
        else:
            self.meanH=62.1242376857/2
        self.numAnchors = len(self.anchors)
        if self.predLineCount>0:
            print('Warning, using hardcoded mean H (yolo_box_detector)')

        in_ch = 3 if 'color' not in config or config['color'] else 1
        norm = config['norm_type'] if "norm_type" in config else None
        if norm is None:
            print('Warning: YoloBoxDetector has no normalization!')
        dilation = config['dilation'] if 'dilation' in config else 1
        #self.cnn, self.scale = vgg.vgg11_custOut(self.predLineCount*5+self.predPointCount*3,batch_norm=batch_norm, weight_norm=weight_norm)
        self.numOutBB = (self.numBBTypes+self.numBBParams)*self.numAnchors
        self.numOutLine = (self.numBBTypes+self.numLineParams)*self.predLineCount
        self.numOutPoint = self.predPointCount*3

        if 'down_layers_cfg' in config:
            layers_cfg = config['down_layers_cfg']
        else:
            layers_cfg=[in_ch,64, 'M', 128, 'M', 256, 256, 'M', 512, 512, 'M', 512, 512]

        self.net_down_modules, down_last_channels = make_layers(layers_cfg, dilation,norm)
        self.final_features=None 
        self.last_channels=down_last_channels
        self.net_down_modules.append(nn.Conv2d(down_last_channels, self.numOutBB+self.numOutLine+self.numOutPoint, kernel_size=1))
        self._hack_down = nn.Sequential(*self.net_down_modules)
        self.scale=1
        for a in layers_cfg:
            if a=='M' or (type(a) is str and a[0]=='D'):
                self.scale*=2
            elif type(a) is str and a[0]=='U':
                self.scale/=2

        if self.predPixelCount>0:
            if 'up_layers_cfg' in config:
                up_layers_cfg =  config['up_layers_cfg']
            else:
                up_layers_cfg=[512, 'U+512', 256, 'U+256', 128, 'U+128', 64, 'U+64']
            self.net_up_modules, up_last_channels = make_layers(up_layers_cfg, 1, norm)
            self.net_up_modules.append(nn.Conv2d(up_last_channels, self.predPixelCount, kernel_size=1))
            self._hack_up = nn.Sequential(*self.net_up_modules)

        #self.base_0 = config['base_0']
        #self.base_1 = config['base_1']

    def forward(self, img):
        #import pdb; pdb.set_trace()
        y = self._hack_down(img)
        if self.forPairing:
            return y[:,:(self.numBBParams+self.numBBTypes)*self.numAnchors,:,:]
        #levels=[img]
        #for module in self.net_down_modules:
        #    levels.append(module(levels[-1]))
        #y=levels[-1]


        #priors_0 = Variable(torch.arange(0,y.size(2)).type_as(img.data), requires_grad=False)[None,:,None]
        priors_0 = torch.arange(0,y.size(2)).type_as(img.data)[None,:,None]
        priors_0 = (priors_0 + 0.5) * self.scale #self.base_0
        priors_0 = priors_0.expand(y.size(0), priors_0.size(1), y.size(3))
        priors_0 = priors_0[:,None,:,:].to(img.device)

        #priors_1 = Variable(torch.arange(0,y.size(3)).type_as(img.data), requires_grad=False)[None,None,:]
        priors_1 = torch.arange(0,y.size(3)).type_as(img.data)[None,None,:]
        priors_1 = (priors_1 + 0.5) * self.scale #elf.base_1
        priors_1 = priors_1.expand(y.size(0), y.size(2), priors_1.size(2))
        priors_1 = priors_1[:,None,:,:].to(img.device)

        anchor = self.anchors
        pred_boxes=[]
        pred_offsets=[] #we seperate anchor predictions here. And compute actual bounding boxes
        for i in range(self.numAnchors):

            offset = i*(self.numBBParams+self.numBBTypes)
            if self.rotation:
                rot_dif = (math.pi/2)*torch.tanh(y[:,3+offset:4+offset,:,:])
            else:
                rot_dif = torch.zeros_like(y[:,3+offset:4+offset,:,:])

            stackedPred = [
                torch.sigmoid(y[:,0+offset:1+offset,:,:]),                #0. confidence
                torch.tanh(y[:,1+offset:2+offset,:,:])*self.scale + priors_1,        #1. x-center
                torch.tanh(y[:,2+offset:3+offset,:,:])*self.scale + priors_0,        #2. y-center
                rot_dif + anchor[i]['rot'],      #3. rotation (radians)
                torch.exp(y[:,4+offset:5+offset,:,:]) * anchor[i]['height'], #4. height (half), I don't think this needs scaled
                torch.exp(y[:,5+offset:6+offset,:,:]) * anchor[i]['width'],  #5. width (half)   as we scale the anchors in training
            ]

            #stackedOffsets = [
            #        y[:,0+offset:1+offset,:,:],
            #        y[:,1+offset:2+offset,:,:],
            #        y[:,2+offset:3+offset,:,:],
            #        y[:,4+offset:5+offset,:,:],
            #        y[:,4+offset:5+offset,:,:]
            #]
            #if self.rotation:
            #    stackedOffsets.append( rot_dif )

            for j in range(self.numBBTypes):
                stackedPred.append(y[:,6+j+offset:7+j+offset,:,:])         #x. class prediction
                #stackedOffsets.append(y[:,6+j+offset:7+j+offset,:,:])         #x. class prediction
            pred_boxes.append(torch.cat(stackedPred, dim=1))
            #pred_offsets.append(torch.cat(stackedOffsets, dim=1))
            pred_offsets.append(y[:,offset:offset+self.numBBParams+self.numBBTypes,:,:])

        bbPredictions = torch.stack(pred_boxes, dim=1)
        offsetPredictions = torch.stack(pred_offsets, dim=1)
        
        bbPredictions = bbPredictions.transpose(2,4).contiguous()#from [batch, anchors, channel, rows, cols] to [batch, anchros, cols, rows, channels]
        bbPredictions = bbPredictions.view(bbPredictions.size(0),bbPredictions.size(1),-1,bbPredictions.size(4))#flatten to [batch, anchors, instances, channel]
        #avg_conf_per_anchor = bbPredictions[:,:,:,0].mean(dim=0).mean(dim=1)
        bbPredictions = bbPredictions.view(bbPredictions.size(0),-1,bbPredictions.size(3)) #[batch, instances+anchors, channel]

        offsetPredictions = offsetPredictions.permute(0,1,3,4,2).contiguous()

        linePreds=[]
        offsetLinePreds=[]
        for i in range(self.predLineCount):
            offset = i*(self.numLineParams+self.numBBTypes) + self.numAnchors*(self.numBBParams+self.numBBTypes)
            stackedPred=[
                torch.sigmoid(y[:,0+offset:1+offset,:,:]),                          #confidence
                torch.tanh(y[:,1+offset:2+offset,:,:])*self.scale + priors_1,       #x-center
                torch.tanh(y[:,2+offset:3+offset,:,:])*self.scale + priors_0,       #y-center
                (math.pi)*torch.tanh(y[:,3+offset:4+offset,:,:]),                 #rotation (radians)
                torch.exp(y[:,4+offset:5+offset,:,:])*self.meanH                    #scale (half-height),
                
            ]
            for j in range(self.numBBTypes):
                stackedPred.append(y[:,5+j+offset:6+j+offset,:,:])         #x. class prediction

            predictions = torch.cat(stackedPred, dim=1)
            predictions = predictions.transpose(1,3).contiguous()#from [batch, channel, rows, cols] to [batch, cols, rows, channels]
            predictions = predictions.view(predictions.size(0),-1,5)#flatten to [batch, instances, channel]
            linePreds.append(predictions)

            offsetLinePreds.append(y[:,offset:offset+self.numLineParams+self.numBBTypes,:,:])

        pointPreds=[]
        for i in range(self.predPointCount):
            offset = i*3 + self.numAnchors*(self.numBBParams+self.numBBTypes)
            predictions = torch.cat([
                torch.sigmoid(y[:,0+offset:1+offset,:,:]),    #confidence
                y[:,1+offset:2+offset,:,:] + priors_1,        #x
                y[:,2+offset:3+offset,:,:] + priors_0         #y
            ], dim=1)
            
            predictions = predictions.transpose(1,3).contiguous()#from [batch, channel, rows, cols] to [batch, cols, rows, channels]
            predictions = predictions.view(predictions.size(0),-1,3)#flatten to [batch, instances, channel]
            pointPreds.append(predictions)

        pixelPreds=None
        if self.predPixelCount>0:
            y2=levels[-2]
            p=-3
            for module in self.net_up_modules[:-1]:
                #print('uping {} , {}'.format(y2.size(), levels[p].size()))
                y2 = module(y2,levels[p])
                p-=1
            pixelPreds = self.net_up_modules[-1](y2)
            



        return bbPredictions, offsetPredictions, linePreds, offsetLinePreds, pointPreds, pixelPreds #, avg_conf_per_anchor

    def summary(self):
        """
        Model summary
        """
        model_parameters = filter(lambda p: p.requires_grad, self.parameters())
        params = sum([np.prod(p.size()) for p in model_parameters])
        print('Trainable parameters: {}'.format(params))

    def setForPairing(self):
        self.forPairing=True
        def save_final(module,input,output):
            self.final_features=output
        self.net_down_modules[-2].register_forward_hook(save_final)
