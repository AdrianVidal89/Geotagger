/**
 * Una foto abierta en el editor.
 */
export class Editor {
    __destroy_into_raw() {
        const ptr = this.__wbg_ptr;
        this.__wbg_ptr = 0;
        EditorFinalization.unregister(this);
        return ptr;
    }
    free() {
        const ptr = this.__destroy_into_raw();
        wasm.__wbg_editor_free(ptr, 0);
    }
    /**
     * Auto de Geotagger: ajusta Exposicion, Iluminaciones, Sombras, Blancos,
     * Negros, Contraste e Intensidad MIDIENDO el resultado.
     *
     * El Auto de LightCraft aplica una receta fija (Contraste +68,
     * Iluminaciones -66, Sombras +52) ajustada para RAW; con JPEG, que ya
     * vienen revelados, comprimia el rango (blancos a ~200, negros
     * levantados) y oscurecia: fotos grises, sin visibilidad. Este revela
     * una version pequena, mira su histograma y corrige cada control hasta
     * llegar a unos objetivos (lazo cerrado), asi que el resultado es el
     * que se busca sea cual sea la foto. Devuelve los valores en JSON.
     * @returns {string}
     */
    auto_tone() {
        let deferred1_0;
        let deferred1_1;
        try {
            const ret = wasm.editor_auto_tone(this.__wbg_ptr);
            deferred1_0 = ret[0];
            deferred1_1 = ret[1];
            return getStringFromWasm0(ret[0], ret[1]);
        } finally {
            wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
        }
    }
    /**
     * El Auto anterior (el de LightCraft), por si se quiere comparar.
     * @returns {string}
     */
    auto_tone_lightcraft() {
        let deferred1_0;
        let deferred1_1;
        try {
            const ret = wasm.editor_auto_tone_lightcraft(this.__wbg_ptr);
            deferred1_0 = ret[0];
            deferred1_1 = ret[1];
            return getStringFromWasm0(ret[0], ret[1]);
        } finally {
            wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
        }
    }
    /**
     * Balance de blancos automatico (escala relativa). Devuelve [temp, tint].
     * @returns {Float64Array}
     */
    auto_wb() {
        const ret = wasm.editor_auto_wb(this.__wbg_ptr);
        var v1 = getArrayF64FromWasm0(ret[0], ret[1]).slice();
        wasm.__wbindgen_free(ret[0], ret[1] * 8, 8);
        return v1;
    }
    /**
     * @returns {boolean}
     */
    bw() {
        const ret = wasm.editor_bw(this.__wbg_ptr);
        return ret !== 0;
    }
    /**
     * Color (OkLab) que tomaria una mascara de rango de color en el punto
     * normalizado (x, y) de la foto. Vacio si cae fuera.
     * @param {number} x
     * @param {number} y
     * @returns {Float64Array}
     */
    color_sample(x, y) {
        const ret = wasm.editor_color_sample(this.__wbg_ptr, x, y);
        var v1 = getArrayF64FromWasm0(ret[0], ret[1]).slice();
        wasm.__wbindgen_free(ret[0], ret[1] * 8, 8);
        return v1;
    }
    /**
     * Revela a resolucion completa y lo codifica (JPEG con `quality` 1..100,
     * 4:4:4; o PNG sin perdida si `png`). Sin metadatos: el servidor copia
     * los del original.
     * @param {boolean} png
     * @param {number} quality
     * @returns {Uint8Array}
     */
    export(png, quality) {
        const ret = wasm.editor_export(this.__wbg_ptr, png, quality);
        if (ret[3]) {
            throw takeFromExternrefTable0(ret[2]);
        }
        var v1 = getArrayU8FromWasm0(ret[0], ret[1]).slice();
        wasm.__wbindgen_free(ret[0], ret[1] * 1, 1);
        return v1;
    }
    /**
     * El mayor recorte que cabe con ese angulo (y proporcion ancho/alto, 0 =
     * la de la foto), en un marco de `w × h`. Devuelve [x0, y0, x1, y1].
     * @param {number} w
     * @param {number} h
     * @param {number} angle
     * @param {number} aspect
     * @returns {Float64Array}
     */
    static fit_crop(w, h, angle, aspect) {
        const ret = wasm.editor_fit_crop(w, h, angle, aspect);
        var v1 = getArrayF64FromWasm0(ret[0], ret[1]).slice();
        wasm.__wbindgen_free(ret[0], ret[1] * 8, 8);
        return v1;
    }
    /**
     * Lee un control por id. Ademas de los de LightCraft: `wb.tempRel` y
     * `wb.tintRel` (balance de blancos relativo, el de las fotos JPEG).
     * @param {string} id
     * @returns {number}
     */
    get(id) {
        const ptr0 = passStringToWasm0(id, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ret = wasm.editor_get(this.__wbg_ptr, ptr0, len0);
        return ret;
    }
    /**
     * @returns {number}
     */
    height() {
        const ret = wasm.editor_height(this.__wbg_ptr);
        return ret >>> 0;
    }
    /**
     * Histograma del ultimo revelado: 256 valores de R, G, B y luminancia.
     * @returns {Uint32Array}
     */
    histogram() {
        const ret = wasm.editor_histogram(this.__wbg_ptr);
        var v1 = getArrayU32FromWasm0(ret[0], ret[1]).slice();
        wasm.__wbindgen_free(ret[0], ret[1] * 4, 4);
        return v1;
    }
    /**
     * Hay algo que guardar (distinto de los ajustes por defecto).
     * @returns {boolean}
     */
    is_default() {
        const ret = wasm.editor_is_default(this.__wbg_ptr);
        return ret !== 0;
    }
    /**
     * @param {string} json
     */
    load_settings_json(json) {
        const ptr0 = passStringToWasm0(json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ret = wasm.editor_load_settings_json(this.__wbg_ptr, ptr0, len0);
        if (ret[1]) {
            throw takeFromExternrefTable0(ret[0]);
        }
    }
    /**
     * Decodifica `bytes`. `orientation`: valor EXIF 1..8 a aplicar, o 0 para
     * usar el del propio archivo. `max_side`: lado mayor de trabajo (0 =
     * resolucion completa; la vista previa trabaja con ~2000 px).
     * @param {Uint8Array} bytes
     * @param {number} orientation
     * @param {number} max_side
     */
    constructor(bytes, orientation, max_side) {
        const ptr0 = passArray8ToWasm0(bytes, wasm.__wbindgen_malloc);
        const len0 = WASM_VECTOR_LEN;
        const ret = wasm.editor_new(ptr0, len0, orientation, max_side);
        if (ret[2]) {
            throw takeFromExternrefTable0(ret[1]);
        }
        this.__wbg_ptr = ret[0];
        EditorFinalization.register(this, this.__wbg_ptr, this);
        return this;
    }
    /**
     * @returns {number}
     */
    out_height() {
        const ret = wasm.editor_out_height(this.__wbg_ptr);
        return ret >>> 0;
    }
    /**
     * Tamano del ultimo revelado.
     * @returns {number}
     */
    out_width() {
        const ret = wasm.editor_out_width(this.__wbg_ptr);
        return ret >>> 0;
    }
    /**
     * @returns {string}
     */
    profile() {
        let deferred1_0;
        let deferred1_1;
        try {
            const ret = wasm.editor_profile(this.__wbg_ptr);
            deferred1_0 = ret[0];
            deferred1_1 = ret[1];
            return getStringFromWasm0(ret[0], ret[1]);
        } finally {
            wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
        }
    }
    /**
     * Revela dentro de `max_w × max_h` y devuelve RGBA de 8 bits. `draft`:
     * calidad de arrastre (mas rapida). `apply_crop`: false enseña la foto
     * entera (herramienta de recorte). `before`: el "antes" (sin ajustes,
     * con el mismo encuadre).
     * @param {number} max_w
     * @param {number} max_h
     * @param {boolean} draft
     * @param {boolean} apply_crop
     * @param {boolean} before
     * @returns {Uint8Array}
     */
    render(max_w, max_h, draft, apply_crop, before) {
        const ret = wasm.editor_render(this.__wbg_ptr, max_w, max_h, draft, apply_crop, before);
        var v1 = getArrayU8FromWasm0(ret[0], ret[1]).slice();
        wasm.__wbindgen_free(ret[0], ret[1] * 1, 1);
        return v1;
    }
    reset() {
        wasm.editor_reset(this.__wbg_ptr);
    }
    /**
     * Cambia un control por id (se recorta a su rango). Devuelve false si el
     * id no existe.
     * @param {string} id
     * @param {number} v
     * @returns {boolean}
     */
    set(id, v) {
        const ptr0 = passStringToWasm0(id, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ret = wasm.editor_set(this.__wbg_ptr, ptr0, len0, v);
        return ret !== 0;
    }
    /**
     * Blanco y negro (tratamiento de LightCraft).
     * @param {boolean} on
     */
    set_bw(on) {
        wasm.editor_set_bw(this.__wbg_ptr, on);
    }
    /**
     * Recorte normalizado (0..1) en el marco girado `angle` grados (horario).
     * @param {number} x0
     * @param {number} y0
     * @param {number} x1
     * @param {number} y1
     * @param {number} angle
     */
    set_crop(x0, y0, x1, y1, angle) {
        wasm.editor_set_crop(this.__wbg_ptr, x0, y0, x1, y1, angle);
    }
    /**
     * Curva por puntos de un canal (`master`, `red`, `green`, `blue`):
     * pares x,y en 0..1 sobre la imagen ya codificada, como en Lightroom.
     * Menos de dos puntos = curva neutra.
     * @param {string} channel
     * @param {Float64Array} xy
     */
    set_curve(channel, xy) {
        const ptr0 = passStringToWasm0(channel, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ptr1 = passArrayF64ToWasm0(xy, wasm.__wbindgen_malloc);
        const len1 = WASM_VECTOR_LEN;
        wasm.editor_set_curve(this.__wbg_ptr, ptr0, len0, ptr1, len1);
    }
    /**
     * Capas de mascara (formato `Mask` de LightCraft, en JSON): cada una con
     * sus formas (pincel, degradado lineal o radial, rango de luminancia o de
     * color, cielo/sujeto/fondo automaticos) y sus ajustes locales.
     * @param {string} json
     */
    set_masks_json(json) {
        const ptr0 = passStringToWasm0(json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ret = wasm.editor_set_masks_json(this.__wbg_ptr, ptr0, len0);
        if (ret[1]) {
            throw takeFromExternrefTable0(ret[0]);
        }
    }
    /**
     * Giro/volteo del usuario, como valor EXIF 1..8 sobre la foto ya orientada.
     * @param {number} exif
     */
    set_orientation(exif) {
        wasm.editor_set_orientation(this.__wbg_ptr, exif);
    }
    /**
     * Pintar la superposicion roja de la mascara `id` (-1: ninguna).
     * @param {number} id
     */
    set_overlay(id) {
        wasm.editor_set_overlay(this.__wbg_ptr, id);
    }
    /**
     * Perfil creativo (`lc.color` = ninguno) y su intensidad 0..200.
     * @param {string} id
     * @param {number} amount
     */
    set_profile(id, amount) {
        const ptr0 = passStringToWasm0(id, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        wasm.editor_set_profile(this.__wbg_ptr, ptr0, len0, amount);
    }
    /**
     * Todos los ajustes en JSON (para deshacer y para pasar la edicion al
     * revelado a resolucion completa).
     * @returns {string}
     */
    settings_json() {
        let deferred1_0;
        let deferred1_1;
        try {
            const ret = wasm.editor_settings_json(this.__wbg_ptr);
            deferred1_0 = ret[0];
            deferred1_1 = ret[1];
            return getStringFromWasm0(ret[0], ret[1]);
        } finally {
            wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
        }
    }
    /**
     * Tamano de la foto de trabajo (ya orientada segun EXIF).
     * @returns {number}
     */
    width() {
        const ret = wasm.editor_width(this.__wbg_ptr);
        return ret >>> 0;
    }
}
if (Symbol.dispose) Editor.prototype[Symbol.dispose] = Editor.prototype.free;

/**
 * Tabla de controles de LightCraft (id, etiqueta, seccion, rango, defecto,
 * paso, decimales, tipo de pista) en JSON.
 * @returns {string}
 */
export function controls_json() {
    let deferred1_0;
    let deferred1_1;
    try {
        const ret = wasm.controls_json();
        deferred1_0 = ret[0];
        deferred1_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
    }
}

/**
 * Perfiles creativos disponibles (ademas de `lc.color`, el neutro).
 * @returns {string}
 */
export function profiles_json() {
    let deferred1_0;
    let deferred1_1;
    try {
        const ret = wasm.profiles_json();
        deferred1_0 = ret[0];
        deferred1_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
    }
}
function __wbg_get_imports() {
    const import0 = {
        __proto__: null,
        __wbg___wbindgen_throw_41e9ee4f547fc59a: function(arg0, arg1) {
            throw new Error(getStringFromWasm0(arg0, arg1));
        },
        __wbindgen_generic_0000000000000001: function(arg0, arg1) {
            // Cast intrinsic for `Ref(String) -> Externref`.
            const ret = getStringFromWasm0(arg0, arg1);
            return ret;
        },
        __wbindgen_init_externref_table: function() {
            const table = wasm.__wbindgen_externrefs;
            const offset = table.grow(4);
            table.set(0, undefined);
            table.set(offset + 0, undefined);
            table.set(offset + 1, null);
            table.set(offset + 2, true);
            table.set(offset + 3, false);
        },
    };
    return {
        __proto__: null,
        "./geotagger_lightcraft_bg.js": import0,
    };
}

const EditorFinalization = (typeof FinalizationRegistry === 'undefined')
    ? { register: () => {}, unregister: () => {} }
    : new FinalizationRegistry(ptr => wasm.__wbg_editor_free(ptr, 1));

function getArrayF64FromWasm0(ptr, len) {
    ptr = ptr >>> 0;
    return getFloat64ArrayMemory0().subarray(ptr / 8, ptr / 8 + len);
}

function getArrayU32FromWasm0(ptr, len) {
    ptr = ptr >>> 0;
    return getUint32ArrayMemory0().subarray(ptr / 4, ptr / 4 + len);
}

function getArrayU8FromWasm0(ptr, len) {
    ptr = ptr >>> 0;
    return getUint8ArrayMemory0().subarray(ptr / 1, ptr / 1 + len);
}

let cachedFloat64ArrayMemory0 = null;
function getFloat64ArrayMemory0() {
    if (cachedFloat64ArrayMemory0 === null || cachedFloat64ArrayMemory0.byteLength === 0) {
        cachedFloat64ArrayMemory0 = new Float64Array(wasm.memory.buffer);
    }
    return cachedFloat64ArrayMemory0;
}

function getStringFromWasm0(ptr, len) {
    return decodeText(ptr >>> 0, len);
}

let cachedUint32ArrayMemory0 = null;
function getUint32ArrayMemory0() {
    if (cachedUint32ArrayMemory0 === null || cachedUint32ArrayMemory0.byteLength === 0) {
        cachedUint32ArrayMemory0 = new Uint32Array(wasm.memory.buffer);
    }
    return cachedUint32ArrayMemory0;
}

let cachedUint8ArrayMemory0 = null;
function getUint8ArrayMemory0() {
    if (cachedUint8ArrayMemory0 === null || cachedUint8ArrayMemory0.byteLength === 0) {
        cachedUint8ArrayMemory0 = new Uint8Array(wasm.memory.buffer);
    }
    return cachedUint8ArrayMemory0;
}

function passArray8ToWasm0(arg, malloc) {
    const ptr = malloc(arg.length * 1, 1) >>> 0;
    getUint8ArrayMemory0().set(arg, ptr / 1);
    WASM_VECTOR_LEN = arg.length;
    return ptr;
}

function passArrayF64ToWasm0(arg, malloc) {
    const ptr = malloc(arg.length * 8, 8) >>> 0;
    getFloat64ArrayMemory0().set(arg, ptr / 8);
    WASM_VECTOR_LEN = arg.length;
    return ptr;
}

function passStringToWasm0(arg, malloc, realloc) {
    if (realloc === undefined) {
        const buf = cachedTextEncoder.encode(arg);
        const ptr = malloc(buf.length, 1) >>> 0;
        getUint8ArrayMemory0().subarray(ptr, ptr + buf.length).set(buf);
        WASM_VECTOR_LEN = buf.length;
        return ptr;
    }

    let len = arg.length;
    let ptr = malloc(len, 1) >>> 0;

    const mem = getUint8ArrayMemory0();

    let offset = 0;

    for (; offset < len; offset++) {
        const code = arg.charCodeAt(offset);
        if (code > 0x7F) break;
        mem[ptr + offset] = code;
    }
    if (offset !== len) {
        if (offset !== 0) {
            arg = arg.slice(offset);
        }
        ptr = realloc(ptr, len, len = offset + arg.length * 3, 1) >>> 0;
        const view = getUint8ArrayMemory0().subarray(ptr + offset, ptr + len);
        const ret = cachedTextEncoder.encodeInto(arg, view);

        offset += ret.written;
        ptr = realloc(ptr, len, offset, 1) >>> 0;
    }

    WASM_VECTOR_LEN = offset;
    return ptr;
}

function takeFromExternrefTable0(idx) {
    const value = wasm.__wbindgen_externrefs.get(idx);
    wasm.__externref_table_dealloc(idx);
    return value;
}

let cachedTextDecoder = new TextDecoder('utf-8', { ignoreBOM: true, fatal: true });
cachedTextDecoder.decode();
const MAX_SAFARI_DECODE_BYTES = 2146435072;
let numBytesDecoded = 0;
function decodeText(ptr, len) {
    numBytesDecoded += len;
    if (numBytesDecoded >= MAX_SAFARI_DECODE_BYTES) {
        cachedTextDecoder = new TextDecoder('utf-8', { ignoreBOM: true, fatal: true });
        cachedTextDecoder.decode();
        numBytesDecoded = len;
    }
    return cachedTextDecoder.decode(getUint8ArrayMemory0().subarray(ptr, ptr + len));
}

const cachedTextEncoder = new TextEncoder();

if (!('encodeInto' in cachedTextEncoder)) {
    cachedTextEncoder.encodeInto = function (arg, view) {
        const buf = cachedTextEncoder.encode(arg);
        view.set(buf);
        return {
            read: arg.length,
            written: buf.length
        };
    };
}

let WASM_VECTOR_LEN = 0;

let wasmModule, wasmInstance, wasm;
function __wbg_finalize_init(instance, module) {
    wasmInstance = instance;
    wasm = instance.exports;
    wasmModule = module;
    cachedFloat64ArrayMemory0 = null;
    cachedUint32ArrayMemory0 = null;
    cachedUint8ArrayMemory0 = null;
    wasm.__wbindgen_start();
    return wasm;
}

async function __wbg_load(module, imports) {
    if (typeof Response === 'function' && module instanceof Response) {
        if (!module.ok) {
            throw new Error(`failed to fetch Wasm: ${module.status} ${module.statusText} fetching '${module.url}'`);
        }

        if (typeof WebAssembly.instantiateStreaming === 'function') {
            try {
                return await WebAssembly.instantiateStreaming(module, imports);
            } catch (e) {
                const validResponse = expectedResponseType(module.type);

                if (validResponse && module.headers.get('Content-Type') !== 'application/wasm') {
                    console.warn("`WebAssembly.instantiateStreaming` failed because your server does not serve Wasm with `application/wasm` MIME type. Falling back to `WebAssembly.instantiate` which is slower. Original error:\n", e);

                } else { throw e; }
            }
        }

        const bytes = await module.arrayBuffer();
        return await WebAssembly.instantiate(bytes, imports);
    } else {
        const instance = await WebAssembly.instantiate(module, imports);

        if (instance instanceof WebAssembly.Instance) {
            return { instance, module };
        } else {
            return instance;
        }
    }

    function expectedResponseType(type) {
        switch (type) {
            case 'basic': case 'cors': case 'default': return true;
        }
        return false;
    }
}

function initSync(module) {
    if (wasm !== undefined) return wasm;


    if (module !== undefined) {
        if (Object.getPrototypeOf(module) === Object.prototype) {
            ({module} = module)
        } else {
            console.warn('using deprecated parameters for `initSync()`; pass a single object instead')
        }
    }

    const imports = __wbg_get_imports();
    if (!(module instanceof WebAssembly.Module)) {
        module = new WebAssembly.Module(module);
    }
    const instance = new WebAssembly.Instance(module, imports);
    return __wbg_finalize_init(instance, module);
}

async function __wbg_init(module_or_path) {
    if (wasm !== undefined) return wasm;


    if (module_or_path !== undefined) {
        if (Object.getPrototypeOf(module_or_path) === Object.prototype) {
            ({module_or_path} = module_or_path)
        } else {
            console.warn('using deprecated parameters for the initialization function; pass a single object instead')
        }
    }

    if (module_or_path === undefined) {
        module_or_path = new URL('geotagger_lightcraft_bg.wasm', import.meta.url);
    }
    const imports = __wbg_get_imports();

    if (typeof module_or_path === 'string' || (typeof Request === 'function' && module_or_path instanceof Request) || (typeof URL === 'function' && module_or_path instanceof URL)) {
        module_or_path = fetch(module_or_path);
    }

    const { instance, module } = await __wbg_load(await module_or_path, imports);

    return __wbg_finalize_init(instance, module);
}

export { initSync, __wbg_init as default };
