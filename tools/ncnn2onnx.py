"""Convierte el modelo de cielo de xiongzhu666 (U^2-Netp, ncnn fp16, MIT) a ONNX.
Solo las operaciones que usa: Convolution(+ReLU), Pooling (max 2x2 ceil),
Interp (bilinear a tamano fijo), Concat, BinaryOp (suma), Sigmoid, Split."""
import sys, struct, numpy as np, onnx
from onnx import helper, TensorProto, numpy_helper

param, binf, out = sys.argv[1:4]
lines = open(param).read().split("\n")
assert lines[0].strip() == "7767517"
nl, nb = map(int, lines[1].split())
data = open(binf, "rb").read(); off = 0
def take_weights(n):
    global off
    tag = struct.unpack_from("<I", data, off)[0]; off += 4
    if tag == 0x01306B47:            # fp16
        w = np.frombuffer(data, dtype=np.float16, count=n, offset=off).astype(np.float32); off += 2 * n
        off = (off + 3) // 4 * 4     # alineado a 4 bytes
    elif tag == 0:
        w = np.frombuffer(data, dtype=np.float32, count=n, offset=off).copy(); off += 4 * n
    else:
        raise SystemExit("etiqueta de pesos desconocida %x" % tag)
    return w
def take_raw_f32(n):
    global off
    w = np.frombuffer(data, dtype=np.float32, count=n, offset=off).copy(); off += 4 * n
    return w

nodes, inits, alias = [], [], {}
name = lambda b: alias.get(b, b)
inp = outname = None
sig_outs = []
for ln in lines[2:2 + nl]:
    t = ln.split()
    typ, lname, ni, no = t[0], t[1], int(t[2]), int(t[3])
    ins = [name(b) for b in t[4:4 + ni]]; outs = t[4 + ni:4 + ni + no]
    kv = {}
    for p in t[4 + ni + no:]:
        k, v = p.split("="); kv[int(k)] = v
    if typ == "Input":
        inp = outs[0]; continue
    if typ == "Split":
        for o in outs: alias[o] = ins[0]
        continue
    if typ == "Convolution":
        oc, k = int(kv[0]), int(kv.get(1, 1)); dil = int(kv.get(2, 1)); st = int(kv.get(3, 1)); pad = int(kv.get(4, 0))
        nw = int(kv[6]); ic = nw // (oc * k * k)
        W = take_weights(nw).reshape(oc, ic, k, k)
        inits.append(numpy_helper.from_array(W, lname + "_w")); cin = [ins[0], lname + "_w"]
        if int(kv.get(5, 0)):
            inits.append(numpy_helper.from_array(take_raw_f32(oc), lname + "_b")); cin.append(lname + "_b")
        act = int(kv.get(9, 0))
        co = outs[0] + ("_pre" if act else "")
        nodes.append(helper.make_node("Conv", cin, [co], name=lname, kernel_shape=[k, k], dilations=[dil, dil], strides=[st, st], pads=[pad] * 4))
        if act == 1: nodes.append(helper.make_node("Relu", [co], [outs[0]], name=lname + "_relu"))
        elif act == 4:
            nodes.append(helper.make_node("Sigmoid", [co], [outs[0]], name=lname + "_sig")); sig_outs.append(outs[0])
        elif act: raise SystemExit("activacion %d" % act)
    elif typ == "Pooling":
        assert kv.get(0, "0") == "0"
        k = int(kv.get(1, 2)); st = int(kv.get(2, 1)); mode = int(kv.get(5, 0))
        nodes.append(helper.make_node("MaxPool", ins, outs, name=lname, kernel_shape=[k, k], strides=[st, st], ceil_mode=1 if mode == 0 else 0))
    elif typ == "Interp":
        assert kv.get(0) == "2", kv
        h, w = int(kv[3]), int(kv[4])
        inits.append(numpy_helper.from_array(np.array([], dtype=np.float32), lname + "_roi"))
        # tamano: [N, C] de la entrada + [h, w] fijos
        nodes.append(helper.make_node("Shape", [ins[0]], [lname + "_shp"], name=lname + "_shape"))
        inits.append(numpy_helper.from_array(np.array([0], dtype=np.int64), lname + "_s0"))
        inits.append(numpy_helper.from_array(np.array([2], dtype=np.int64), lname + "_s2"))
        nodes.append(helper.make_node("Slice", [lname + "_shp", lname + "_s0", lname + "_s2"], [lname + "_nc"], name=lname + "_slice"))
        inits.append(numpy_helper.from_array(np.array([h, w], dtype=np.int64), lname + "_hw"))
        nodes.append(helper.make_node("Concat", [lname + "_nc", lname + "_hw"], [lname + "_sizes"], name=lname + "_cat", axis=0))
        nodes.append(helper.make_node("Resize", [ins[0], lname + "_roi", "", lname + "_sizes"], outs, name=lname, mode="linear",
                                      coordinate_transformation_mode="half_pixel" if kv.get(6, "0") == "0" else "align_corners"))
    elif typ == "Concat":
        nodes.append(helper.make_node("Concat", ins, outs, name=lname, axis=int(kv.get(0, 0)) + 1))
    elif typ == "BinaryOp":
        assert kv.get(0, "0") == "0", kv
        nodes.append(helper.make_node("Add", ins, outs, name=lname))
    elif typ == "Sigmoid":
        nodes.append(helper.make_node("Sigmoid", ins, outs, name=lname)); sig_outs.append(outs[0])
    else:
        raise SystemExit("capa no soportada: " + typ)
print("bytes leidos", off, "de", len(data))
assert abs(off - len(data)) < 8
graph = helper.make_graph(nodes, "skyseg", [helper.make_tensor_value_info(inp, TensorProto.FLOAT, [1, 3, 384, 384])],
                          [helper.make_tensor_value_info(sig_outs[0], TensorProto.FLOAT, [1, 1, 384, 384])], inits)
m = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
m.ir_version = 8
onnx.checker.check_model(m)
onnx.save(m, out)
print("salidas sigmoide:", sig_outs, "-> usando", sig_outs[0])
