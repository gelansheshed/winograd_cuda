#!/usr/bin/python

# Copyright 2015-2016 Nervana Systems Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import division
import numpy as np
from struct import pack, unpack
# from neon import logger as neon_logger

def ceil_div(x, y):
    return -(-x // y)

def out_dim(S, X, padding, strides):
    return ceil_div(X - S + 1 + 2 * padding, strides)

def strip_mantissa(val):
    i = unpack('I', pack('f', val))[0] & 0x7f800000
    f = unpack('f', pack('I', i))[0]
    return f

def quantize(ary, bits):
    maxval = float(np.max(np.absolute(ary)))
    scale  = strip_mantissa(maxval) / float(1 << bits - 2)
    ary    = np.around(ary * (1.0 / scale)).astype(np.int64)
    return ary, np.float32(scale)

######### Direct Convolution #########

def fconv_slice(q, S, X, padding, strides):
    f1 = 0
    f2 = S - 1
    x1 = q * strides - padding
    x2 = x1 + f2
    if x1 < 0:
        f1 = -x1
        x1 = 0
    if x2 >= X:
        dif = x2 - X + 1
        f2 -= dif
        x2 -= dif
    return (slice(f1, f2 + 1), slice(x1, x2 + 1), f2 - f1 + 1)

def bconv_slice(x, S, Q, padding, strides):
    qs = x - (S - padding - 1)
    firstF = None
    for s in range(S): #TODO remove loop logic here.
        q = qs + s
        if q % strides == 0:
            q //= strides
            if q >= 0 and q < Q:
                if firstF is None:
                    firstF = s
                    firstE = q
                lastF = s
                lastE = q
    if firstF is None:
        return (slice(0,0,1), slice(0,0,1), 0)
    return (slice(firstF,lastF + 1,strides), slice(firstE,lastE + 1,1), 0)

def xprop_direct(I, F, O, padding, strides, backward=False):

    if all(x == 1 for x in F.shape[1:3]):
        C = F.shape[0]
        if backward:
            # CxHWN = CxK . KxHWN
            O[:] = np.dot( F.reshape((C, -1)),   I.reshape((C, -1)) ).reshape((O.shape))
        else:
            # KxHWN = CxK.T . CxHWN
            O[:] = np.dot( F.reshape((C, -1)).T, I.reshape((C, -1)) ).reshape((O.shape))
        return

    if backward:
        # C <=> K and mirror R,S
        F = np.transpose(F[:,::-1,::-1,:], (3,1,2,0)).copy()
        xconv_slice = bconv_slice
    else:
        xconv_slice = fconv_slice

    C, Y, X, N = I.shape
    C, R, S, K = F.shape
    K, P, Q, N = O.shape

    qSlice = [ xconv_slice(q, S, X, padding[0], strides[0]) for q in range(Q) ]

    for p in range(P):
        sliceR, sliceY, _ = xconv_slice(p, R, Y, padding[1], strides[1])

        for q in range(Q):
            sliceS, sliceX, _ = qSlice[q]

            slicedF = F[:,sliceR,sliceS,:].reshape((-1, K))
            slicedI = I[:,sliceY,sliceX,:].reshape((-1, N))

            O[:,p,q,:] = np.dot( slicedF.T,  slicedI )

def updat_direct(I, E, U, padding, strides):

    C, Y, X, N = I.shape
    K, P, Q, N = E.shape
    C, R, S, K = U.shape

    if all(x == 1 for x in (R, S)):
        # CxK = CxHWN . KxHWN.T
        U[:] = np.dot( I.reshape((C, -1)), E.reshape((K, -1)).T ).reshape((U.shape))
        return

    U.fill(0.0)

    qSlice = [ fconv_slice(q, S, X, padding[0], strides[0]) for q in range(Q) ]

    for p in range(P):
        sliceR, sliceY, rlen = fconv_slice(p, R, Y, padding[1], strides[1])

        for q in range(Q):
            sliceS, sliceX, slen = qSlice[q]

            slicedI = I[:,sliceY,sliceX,:].reshape((-1, N))
            slicedE = E[:,p,q,:]

            U[:,sliceR,sliceS,:] += np.dot( slicedI,  slicedE.T ).reshape((C, rlen, slen, K))

######### Winograd Convolution #########

I_2x2_3x3 = np.array([
    [ 1.0,  0.0, -1.0,  0.0 ],
    [ 0.0,  1.0,  1.0,  0.0 ],
    [ 0.0, -1.0,  1.0,  0.0 ],
    [ 0.0,  1.0,  0.0, -1.0 ]])
F_2x2_3x3 = np.array([
    [ 1.0,  0.0, 0.0 ],
    [ 0.5,  0.5, 0.5 ],
    [ 0.5, -0.5, 0.5 ],
    [ 0.0,  0.0, 1.0 ]])
O_2x2_3x3 = np.array([
    [ 1.0, 1.0,  1.0,  0.0 ],
    [ 0.0, 1.0, -1.0, -1.0 ]]) #, dtype=np.float32

half    = np.float(0.5)
quarter = np.float(0.25)

def trans_I_2x2_3x3(Iw, I, minimal=False):
    if minimal:

        T0 = np.empty((4,4))
        T1 = np.empty((4,4))

        for O, I in ((T0, I), (T1, T0.T)):

            O[0,:] = I[0,:] - I[2,:]
            O[1,:] = I[1,:] + I[2,:]
            O[2,:] = I[2,:] - I[1,:]
            O[3,:] = I[1,:] - I[3,:]

        Iw[:] = T1.T

    else:
        Iw[:] = np.dot( np.dot(I_2x2_3x3, I), I_2x2_3x3.T )

def trans_F_2x2_3x3(Fw, F, minimal=False):
    if minimal:

        T0 = np.empty((4,3))
        T1 = np.empty((4,4))

        for O, I in ((T0, F), (T1, T0.T)):

            t0 = (I[0,:] + I[2,:]) * 0.5

            O[0,:] = I[0,:]
            O[1,:] = t0 + I[1,:] * 0.5
            O[2,:] = t0 - I[1,:] * 0.5
            O[3,:] = I[2,:]

        Fw[:] = T1.T
    else:
        Fw[:] = np.dot( np.dot(F_2x2_3x3, F), F_2x2_3x3.T )

def trans_O_2x2_3x3(Mw, minimal=False):
    if minimal:

        T0 = np.empty((2,4))
        T1 = np.empty((2,2))

        for O, I in ((T0, Mw), (T1, T0.T)):

            t0 = I[0,:] + I[1,:]
            t1 = I[1,:] - I[2,:]

            O[0,:] = t0 + I[2,:]
            O[1,:] = t1 - I[3,:]

        return T1.T

    else:
        return np.dot( np.dot(O_2x2_3x3, Mw), O_2x2_3x3.T )


I_3x3_2x2 = np.array([
    [ 1.0,  0.0, -1.0,  0.0 ],
    [ 0.0,  1.0,  1.0,  0.0 ],
    [ 0.0, -1.0,  1.0,  0.0 ],
    [ 0.0, -1.0,  0.0,  1.0 ]])
F_3x3_2x2 = np.array([
    [ 1.0,  0.0 ],
    [ 0.5,  0.5 ],
    [ 0.5, -0.5 ],
    [ 0.0,  1.0 ]])
O_3x3_2x2 = np.array([
    [ 1.0, 1.0,  1.0,  0.0 ],
    [ 0.0, 1.0, -1.0,  0.0 ],
    [ 0.0, 1.0,  1.0,  1.0 ]])

def trans_I_3x3_2x2(Iw, I, minimal=False):
    if minimal:

        T0 = np.empty((4,4))
        T1 = np.empty((4,4))

        for O, I in ((T0, I), (T1, T0.T)):

            O[0,:] = I[0,:] - I[2,:]
            O[1,:] = I[1,:] + I[2,:]
            O[2,:] = I[2,:] - I[1,:]
            O[3,:] = I[3,:] - I[1,:]

        Iw[:] = T1.T

    else:
        Iw[:] = np.dot( np.dot(I_3x3_2x2, I), I_3x3_2x2.T )

def trans_F_3x3_2x2(Fw, F, minimal=False):
    if minimal:

        T0 = np.empty((4,2))
        T1 = np.empty((4,4))

        for O, I in ((T0, F), (T1, T0.T)):

            O[0,:] =  I[0,:]
            O[1,:] = (I[0,:] + I[1,:]) * 0.5
            O[2,:] = (I[0,:] - I[1,:]) * 0.5
            O[3,:] =  I[1,:]

        Fw[:] = T1.T

        # x0  =  half*F[0,0]
        # x1  =  half*F[1,1]
        # B0  =  half*F[1,0] + x0
        # B1  =  half*F[0,1] + x1
        # C0  = -half*F[1,0] + x0
        # C1  =  half*F[0,1] - x1
        # x2  =  half*B0
        # x3  =  half*C0
        # Fw[0,0] =  F[0,0]
        # Fw[0,1] =  half*F[0,1] + x0
        # Fw[0,2] = -half*F[0,1] + x0
        # Fw[0,3] =  F[0,1]
        # Fw[3,0] =  F[1,0]
        # Fw[3,1] =  half*F[1,0] + x1
        # Fw[3,2] =  half*F[1,0] - x1
        # Fw[3,3] =  F[1,1]
        # Fw[1,0] =  B0
        # Fw[1,1] =  half*B1 + x2
        # Fw[1,2] = -half*B1 + x2
        # Fw[1,3] =  B1
        # Fw[2,0] =  C0
        # Fw[2,1] =  half*C1 + x3
        # Fw[2,2] = -half*C1 + x3
        # Fw[2,3] =  C1
    else:
        Fw[:] = np.dot( np.dot(F_3x3_2x2, F), F_3x3_2x2.T )

def trans_O_3x3_2x2(Mw, minimal=False):

    if minimal:

        T0 = np.empty((3,4))
        T1 = np.empty((3,3))

        for O, I in ((T0, Mw), (T1, T0.T)):

            t0 = I[1,:] + I[2,:]

            O[0,:] = t0 + I[0,:]
            O[1,:] = I[1,:] - I[2,:]
            O[2,:] = t0 + I[3,:]

        return T1.T
    else:
        return np.dot( np.dot(O_3x3_2x2, Mw), O_3x3_2x2.T )

def image_slice(x, X, B, D, pad=0):
    start = x * B - pad
    stop  = start + D
    pad = [0,0]
    if start < 0:
        pad[0] = -start
        start = 0
    if stop - 1 >= X:
        pad[1] = stop - X
    return start, stop, pad



def xprop_winograd(I, F, O, padding, minimal=False, backward=False):

    if backward:
        # C <=> K and mirror R,S
        F = np.transpose(F[:,::-1,::-1,:], (3,1,2,0)).copy()
        # Invert padding
        padding = [2 - p for p in padding]

    C, Y, X, N = I.shape
    K, P, Q, N = O.shape

    B = 2
    D = B + 2
    Yw = ceil_div(P, B)
    Xw = ceil_div(Q, B)

    Fw = np.empty((D,D,C,K))
    Iw = np.empty((D,D,C,Yw,Xw,N))
    Mw = np.empty((D,D,K,Yw,Xw,N)) #, dtype=np.float32

    # Transform Filters
    for c in range(C):
        for k in range(K):
            trans_F_2x2_3x3(Fw[:,:,c,k], F[c,:,:,k], minimal)

    file = open('Fw.txt', 'w')
    for a in range(D):
        for b in range(D):
            for c in range(C):
                for d in range(K):
                    file.write('{};'.format(Fw[a][b][c][d]))
    file.close()

    # Iterate over image transform dimensions and slice out tiles of the image
    for y in range(Yw):
        start_y, stop_y, pad_y = image_slice(y, Y, B, D, padding[0])

        for x in range(Xw):
            start_x, stop_x, pad_x = image_slice(x, X, B, D, padding[1])
            # Sometimes stop_y > 4 !!!
            sliceI = I[:, start_y:stop_y, start_x:stop_x, :]

            # add any zero padding if needed
            print "pad_x={}, start_x={}, stop_x={}".format(pad_x,start_x,stop_x)
            print "pad_y={}, start_y={}, stop_y={}".format(pad_y,start_y,stop_y)
            if any(pad_y) or any(pad_x):
                sliceI = np.pad(sliceI, ((0,0), pad_y, pad_x, (0,0)), 'constant')

            file = open('sliceI.txt', 'w')
            for a in range(C):
                for b in range(Y):
                    for c in range(X):
                        for d in range(N):
                            file.write('{};'.format(sliceI[a][b][c][d]))
            file.close()

            # Apply the Image transform
            file2 = open('sliceI_mat_ref.txt', 'w')
            for c in range(C):
                for n in range(N):
                    print "c={},n={}".format(c,n)
                    print "slideI = {}".format(sliceI[c,:,:,n])

                    file2.write("sliceI (c={};n={}):\n".format(c,n))
                    file2.write("{}\n".format(sliceI[c,:,:,n]))
                    trans_I_2x2_3x3(Iw[:,:,c,y,x,n], sliceI[c,:,:,n], minimal)
                    print "Iw = {}".format(Iw[:,:,c,y,x,n])
            file2.close()
    file = open('Iw.txt', 'w')
    for a in range(D):
        for b in range(D):
            for c in range(C):
                for d in range(Yw):
                    for e in range(Xw):
                        for f in range(N):
                            file.write('{};'.format(Iw[a][b][c][d][e][f]))
    file.close()

    # Fw, scaleF = quantize(Fw, 8)
    # Iw, scaleI = quantize(Iw, 8)

    # Fw = Fw.astype(np.float32)
    # Iw = Iw.astype(np.float32)

    # Batched gemm for the pointwise multiplication step
    for s in range(D):
        for t in range(D):
            # [K,Yw,Xw,N] = [C,K].T . [C,YwXwN]
            Mw[s,t] = np.dot( Fw[s,t].T, Iw[s,t].reshape(C, -1) ).reshape((K,Yw,Xw,N))
    file = open('Mw.txt', 'w')
    for a in range(D):
        for b in range(D):
            for c in range(K):
                for d in range(Yw):
                    for e in range(Xw):
                        for f in range(N):
                            file.write('{};'.format(Mw[a][b][c][d][e][f]))
    file.close()

    # Mw = Mw.astype(np.float32) * scaleF * scaleI

    # Mw = Mw.astype(np.float32)

    # Iterate over the convovled result in the pointwise space and apply inverse transform
    for y in range(Yw):
        p    = y * B
        plen = 2 if p + 1 < P else 1
        for x in range(Xw):
            q  = x * B
            qlen = 2 if q + 1 < Q else 1
            for k in range(K):
                for n in range(N):
                    # Toss out any points that don't fit
                    O[k,p:p + plen,q:q + qlen,n] = trans_O_2x2_3x3(Mw[:,:,k,y,x,n], minimal)[0:plen,0:qlen]



def updat_winograd(I, E, U, padding, minimal=False, inner=False):

    C, Y, X, N = I.shape
    K, P, Q, N = E.shape

    B = 2
    D = B + 2
    Yw = ceil_div(P, B)
    Xw = ceil_div(Q, B)

    Iw = np.empty((D,D,N,C))
    Ew = np.empty((D,D,N,K))
    if inner:
        Mw = np.empty((D,D,C,K))
        U.fill(0.0)
    else:
        Mw = np.zeros((D,D,C,K))

    for y in range(Yw):
        start_y, stop_y, pad_y = image_slice(y, Y, B, D, padding[0])
        start_p, stop_p, pad_p = image_slice(y, P, B, B)

        for x in range(Xw):
            start_x, stop_x, pad_x = image_slice(x, X, B, D, padding[1])
            start_q, stop_q, pad_q = image_slice(x, Q, B, B)

            sliceI = I[:, start_y:stop_y, start_x:stop_x, :]
            sliceE = E[:, start_p:stop_p, start_q:stop_q, :]

            if any(pad_y) or any(pad_x):
                sliceI = np.pad(sliceI, ((0,0), pad_y, pad_x, (0,0)), 'constant')

            if any(pad_p) or any(pad_q):
                sliceE = np.pad(sliceE, ((0,0), pad_p, pad_q, (0,0)), 'constant')

            for c in range(C):
                for n in range(N):
                    trans_I_3x3_2x2(Iw[:,:,n,c], sliceI[c,:,:,n], minimal)

            # print y,x
            # print Iw[:,:,0,0].reshape((16,))
            # exit()

            for k in range(K):
                for n in range(N):
                    trans_F_3x3_2x2(Ew[:,:,n,k], sliceE[k,:,:,n], minimal)

            # print Ew[:,:,0,0].reshape((16,))

            for s in range(D):
                for t in range(D):
                    # [C,K] += [N,C].T . [N,K]
                    if inner:
                        Mw[s,t]  = np.dot( Iw[s,t].T, Ew[s,t] )
                    else:
                        Mw[s,t] += np.dot( Iw[s,t].T, Ew[s,t] )

            # Transform can be applied in inner or outer loop
            if inner:
                for c in range(C):
                    for k in range(K):
                        U[c,:,:,k] += trans_O_3x3_2x2(Mw[:,:,c,k], minimal)

    # outer loop transform
    if not inner:
        for c in range(C):
            for k in range(K):
                U[c,:,:,k] = trans_O_3x3_2x2(Mw[:,:,c,k], minimal)



### Test Code ###

np.set_printoptions(threshold=8192 * 4, linewidth=600, formatter={'float':lambda x: "%6.3f" % x})
Ft = np.ones((3,3));
Fwt = np.empty((4,4));
k=0
# for i in range(0,3):
#     for j in range(0,3):
#         Ft[i][j] = k;
#         k+=1;
minimal = 1
trans_F_2x2_3x3(Fwt, Ft, minimal)
It = np.ones((4,4));
Iwt = np.empty((4,4));
k=0
for i in range(0,4):
    for j in range(0,4):
        It[i][j] = k;
        k+=1;
trans_I_2x2_3x3(Iwt, It, minimal)

Mwt = np.ones((4,4));
Mt = np.empty((2,2));
k=0
# for i in range(0,4):
#     for j in range(0,4):
#         Mwt[i][j] = k;
#         k+=1;
Mt=trans_O_2x2_3x3(Mwt, minimal)

ones = 1
N    = 32
C, K = 32, 32
Y, X = 4, 4
R, S = 3, 3     # Fixed winograd dim
strides = 1, 1  # Fixed winograd dim
padding = 1, 1  # 0-2

P = out_dim(R, Y, padding[0], strides[0])
Q = out_dim(S, X, padding[1], strides[1])

dimI = (C,Y,X,N)
dimF = (C,R,S,K)
dimO = (K,P,Q,N)

if ones:
    I  = np.ones(dimI)
    F  = np.ones(dimF)
    E  = np.ones(dimO)

    # for c in range(C):
    #     for n in range(N):
    #         I[c,:,:,n] = c+1 #np.arange(1+c,37+c).reshape((6,6))

    # for k in range(K):
    #     for n in range(N):
    #         E[k,:,:,n] = k+1 #np.arange(1+k,17+k).reshape((4,4))

else:
    I  = np.maximum(np.random.uniform(-1.0, 1.0, dimI), 0)
    F  = np.random.normal(0.0, 0.1, dimF)
    #F  = np.random.uniform(-1.0, 1.0, dimF)
    E  = np.random.uniform(-1.0, 1.0, dimO)

k=0
for a in range(dimI[0]):
    for b in range(dimI[1]):
        for c in range(dimI[2]):
            for d in range(dimI[3]):
                I[a][b][c][d] = k
                k += 1

for a in range(dimF[0]):
    for b in range(dimF[1]):
        for c in range(dimF[2]):
            for d in range(dimF[3]):
                F[a][b][c][d] = k
                k += 1

Od = np.empty(dimO)
Ow = np.empty(dimO) #, dtype=np.float32

Bd = np.empty(dimI)
Bw = np.empty(dimI) #, dtype=np.float32

Ud = np.empty(dimF)
Uw = np.empty(dimF)

xprop_direct(I, F, Od, padding, strides)
xprop_winograd(I, F, Ow, padding, minimal=minimal)

file = open('Ow.txt','w')
for a in range(dimO[0]):
    for b in range(dimO[1]):
        for c in range(dimO[2]):
            for d in range(dimO[3]):
                file.write('{};'.format(Ow[a][b][c][d]))
file.close()

xprop_direct(E, F, Bd, padding, strides, backward=True)
xprop_winograd(E, F, Bw, padding, minimal=minimal, backward=True)

updat_direct(I, E, Ud, padding, strides)
updat_winograd(I, E, Uw, padding, minimal=minimal, inner=True)

difO = Od - Ow
difB = Bd - Bw
difU = Ud - Uw

#neon_logger.display(abs(difO).max() / Od.max())
#neon_logger.display(abs(difB).max() / Bd.max())
#neon_logger.display(abs(difU).max() / Ud.max())

print abs(difO).max() / Od.max()
print abs(difB).max() / Bd.max()
print abs(difU).max() / Ud.max()

# print Bd[0,:,:,0]
# print Bw[0,:,:,0]

# print Ud[0,:,:,0]
# print Uw[0,:,:,0]
# print difU[0,:,:,0]
