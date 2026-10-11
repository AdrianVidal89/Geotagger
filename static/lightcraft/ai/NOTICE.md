Modelos y motor de inferencia del editor (corren en el navegador, nunca en el NAS):

- `ort.wasm.min.mjs`, `ort-wasm-simd-threaded.*`: ONNX Runtime Web 1.22.0
  (https://github.com/microsoft/onnxruntime), licencia MIT.
- `u2netp.onnx`: U^2-Net (version ligera) para detectar el sujeto principal.
  Qin et al., https://github.com/xuebinqin/U-2-Net, licencia Apache-2.0
  (ONNX tal y como lo distribuye rembg, https://github.com/danielgatis/rembg).
- `skyseg.onnx`: U^2-Net entrenado para cielos por xiongzhu666
  (https://github.com/xiongzhu666/Sky-Segmentation-and-Post-processing),
  licencia MIT (ver LICENSE-skyseg-MIT). Convertido de ncnn a ONNX con
  `tools/ncnn2onnx.py`, sin cambiar los pesos.
