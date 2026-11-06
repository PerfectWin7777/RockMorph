/**
 * RockMorph 3D Explorer — WebGL Render Engine (GLSL Shader Edition)
 *
 * Responsibilities
 * ----------------
 * - Initialize and run the Three.js scene, camera, lights, and render loop.
 * - Use a custom GLSL ShaderMaterial to guarantee robust, scale-independent
 *   and version-agnostic lighting on all platforms.
 * - Expose processPythonCommand(command) as the single entry point for all
 *   messages coming from the PyQt panel via QWebChannel.
 * - Maintain a sceneObjects registry so any object can be toggled, updated,
 *   or removed by its element_id without searching the scene graph.
 *
 * Coordinate conventions
 * ----------------------
 * - X → East, Y → North, Z → Up (right-handed, matches geo conventions).
 * - All terrain vertices are centered around (0,0) by the Python engine before
 *   being sent here, so Three.js never deals with large absolute coordinates.
 *
 * Authors: RockMorph contributors
 */

"use strict";

// ---------------------------------------------------------------------------
// Scene globals
// ---------------------------------------------------------------------------

let scene, camera, renderer, controls;
let axesScene, axesCamera;
let axesSceneVisible = true;

// ---------------------------------------------------------------------------
// Scene object registry
// Keyed by element_id (string). Value: { mesh, type, visible }
// ---------------------------------------------------------------------------
const sceneObjects = {};

// ---------------------------------------------------------------------------
// Terrain state — cached for real-time Z-scale updates
// ---------------------------------------------------------------------------
let originalDEMValues = null;   // 2D array [row][col] of raw elevation floats
let cachedModelWidth = 100.0;   // Cached geographical width of the active DEM
let cachedModelHeight = 100.0;  // Cached geographical height of the active DEM
let elevationStats = { min: 0, max: 1 };
let spatialOffsets = { x: 0, y: 0, z: 0 };
let baseExaggeration = 1.0;    // auto-computed once per DEM load
let currentZScale = 1.5;
let blockBaseVisible = true; // Authoritative visibility state for walls and sole
let legendVisibleUserPref = true; // Authoritative user visibility preference for colorbar
// Wall vertex index mappings for synchronized Z-scale updates.
let wallVertexMappings = [];

let currentShadingMode = 0; //  0: Phong, 1: Gouraud, 2: Flat 

// Cached reference to the wall mesh for Z-scale sync
let wallMesh = null;

// Current colormap state
let currentColormapName = "terrain";
let currentColormapReverse = false;
let currentClassColors = [];   // Array of hex strings representing custom class colors
let currentClassBounds = [];   // Array of floats representing upper bounds of classes (length N-1)
let colorMode = "colormap";    // Active color state: "colormap", "solid", "classified"
let colorBoundsAuto = true;
let colorBoundsMin = 0;
let colorBoundsMax = 1;
let perspectiveCamera, orthographicCamera;

// Current base thickness as fraction of max dimension (default 5%)
let baseThicknessFraction = 0.05;

// Sky gradient colors
let skyColorTop = new THREE.Color(0x1a2a3a);
let skyColorBottom = new THREE.Color(0x3d6080);

// Block base colors (initialized to new neutral grey defaults)
let wallsColor = new THREE.Color(0x6b6b6b);  
let baseColor = new THREE.Color(0x3d3d3d);

// Scale helper for dynamic lighting distance
let modelMaxDim = 100.0;
let scaleFactor = 1.0;

// ---------------------------------------------------------------------------
// Global Lighting & Shading Variables (Authoritative uniform data source)
// ---------------------------------------------------------------------------
let sunDirection = new THREE.Vector3(0.5, 0.5, 1.0).normalize();
let sunColor = new THREE.Color(0xffffff);
let sunIntensity = 1.5;
let ambientColor = new THREE.Color(0xffffff);
let ambientIntensity = 0.3;

// Physical terrain properties for scientific visualization
let globalRoughness = 0.5;      // 0.0 (Lambertian) to 1.0 (highly matte/rough rock)
let globalSlopeContrast = 0.3;  // 0.0 (no effect) to 1.0 (accentuate steep incised valleys)
let globalMultidirectional = false; // Toggle between single sun and GIS 4-way hillshading

// ---------------------------------------------------------------------------
// 1. TERRAIN SHADERS
// ---------------------------------------------------------------------------
//
// Two rendering styles share one shader:
//
//   uRenderStyle 0 — "Classic hillshade": the original RockMorph lighting
//                    (Lambert/Oren-Nayar sun, hemisphere ambient, slope
//                    darkening, no tone mapping). Kept for figures that
//                    must match earlier exports.
//   uRenderStyle 1 — "Realistic": linear-space lighting with
//                    * per-pixel normals from the high-resolution height
//                      texture, using the true cell size and the current
//                      vertical exaggeration (the old code used a fixed
//                      factor of 30 whatever the DEM);
//                    * soft cast shadows and sky-view-factor ambient
//                      occlusion, both BAKED into uShadowAO (see the bake
//                      shader below) so the per-frame cost is one fetch;
//                    * sky + ground-bounce ambient, ACES filmic tone mapping
//                      and sRGB output;
//                    * aerial perspective (distance haze);
//                    * optional "natural" land-cover colouring from
//                      elevation, true slope and aspect.
//
// Heights in uHeightmap are normalised: 0 = z_min, 1 = z_max. In model
// space the terrain spans uModelSize (x east, y north) and a normalised
// height of 1 stands uHeightRange units high (exaggeration included);
// uRealHeightRange is the same height without exaggeration, used where the
// TRUE slope matters (land cover).

const terrainVertexShader = `
    attribute vec3 color;

    uniform vec3 uSunDirection;
    uniform vec3 uSunColor;
    uniform float uSunIntensity;
    uniform float uAmbientIntensity;
    uniform bool uMultidirectional;
    uniform int uShadingMode;

    varying vec3 vNormal;
    varying vec3 vWorldNormal;
    varying vec3 vColor;
    varying vec2 vUv;
    varying vec3 vViewPosition;
    varying vec3 vGouraudLighting; // per-vertex lighting (classic Gouraud mode)
    varying vec3 vModelPosition;   // terrain mesh is not transformed: model = world

    void main() {
        vModelPosition = position;
        vNormal = normalize(normalMatrix * normal);
        vWorldNormal = normalize(normal);
        vColor = color;
        vUv = uv;

        vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
        vViewPosition = -mvPosition.xyz;

        vGouraudLighting = vec3(1.0);
        if (uShadingMode == 1) {
            vec3 n = normalize(vWorldNormal);
            vec3 skyColor = vec3(0.55, 0.68, 0.85);
            vec3 groundColor = vec3(0.20, 0.17, 0.13);
            vec3 ambient = mix(groundColor, skyColor, n.z * 0.5 + 0.5) * uAmbientIntensity;
            vec3 L0 = normalize(uSunDirection);
            float diffuseTerm = max(dot(n, L0), 0.0);
            if (uMultidirectional) {
                vec3 L1 = vec3(-L0.y, L0.x, L0.z);
                vec3 L2 = vec3(-L0.x, -L0.y, L0.z);
                vec3 L3 = vec3(L0.y, -L0.x, L0.z);
                diffuseTerm = max(dot(n, L0), 0.0) * 0.4 + max(dot(n, L1), 0.0) * 0.2
                            + max(dot(n, L2), 0.0) * 0.2 + max(dot(n, L3), 0.0) * 0.2;
            }
            vGouraudLighting = ambient + (uSunColor * diffuseTerm * uSunIntensity);
        }

        gl_Position = projectionMatrix * mvPosition;
    }
`;

const terrainFragmentShader = `
    uniform vec3 uSunDirection;
    uniform vec3 uSunColor;
    uniform float uSunIntensity;
    uniform vec3 uAmbientColor;
    uniform float uAmbientIntensity;
    uniform vec3 uSolidColor;
    uniform bool uUseVertexColors;

    uniform float uRoughness;
    uniform float uSlopeContrast;
    uniform bool uMultidirectional;
    uniform sampler2D uHeightmap;
    uniform vec2 uTextureSize;
    uniform float uZScale;
    uniform int uShadingMode;

    uniform int uRenderStyle;          // 0 classic, 1 realistic
    uniform vec2 uModelSize;           // terrain extent in model units
    uniform float uHeightRange;        // model units for a normalised height of 1 (exaggerated)
    uniform float uRealHeightRange;    // same, without vertical exaggeration
    uniform sampler2D uShadowAO;       // r = sun visibility, g = sky-view factor
    uniform bool uCastShadows;
    uniform float uAOStrength;         // 0 none … 1 full sky-view factor
    uniform float uAtmosphere;         // aerial perspective strength
    uniform float uExposure;
    uniform vec3 uSkyTint;             // sky colour lighting the shaded slopes
    uniform vec3 uHazeColor;
    uniform bool uLandCover;
    uniform float uTreeLine;           // normalised elevation
    uniform float uSnowLine;           // normalised elevation
    uniform float uRockSlope;          // degrees: bare rock above this slope
    uniform vec3 uForestColor;         // land-cover colours, sRGB as picked
    uniform vec3 uGrassColor;
    uniform vec3 uRockColor;
    uniform vec3 uSnowColor;

    varying vec3 vNormal;
    varying vec3 vWorldNormal;
    varying vec3 vColor;
    varying vec2 vUv;
    varying vec3 vViewPosition;
    varying vec3 vGouraudLighting;
    varying vec3 vModelPosition;

    float heightAt(vec2 uv) { return texture2D(uHeightmap, uv).r; }

    // Central-difference normal of the height texture for a given vertical
    // range: horizontal spacing = 2 texels in model units.
    vec3 heightNormal(vec2 uv, float verticalRange) {
        vec2 t = 1.0 / uTextureSize;
        float dzdx = (heightAt(uv + vec2(t.x, 0.0)) - heightAt(uv - vec2(t.x, 0.0)))
                     * verticalRange / (2.0 * t.x * uModelSize.x);
        float dzdy = (heightAt(uv + vec2(0.0, t.y)) - heightAt(uv - vec2(0.0, t.y)))
                     * verticalRange / (2.0 * t.y * uModelSize.y);
        return normalize(vec3(-dzdx, -dzdy, 1.0));
    }

    vec3 srgbToLinear(vec3 c) { return pow(max(c, 0.0), vec3(2.2)); }
    vec3 linearToSrgb(vec3 c) { return pow(max(c, 0.0), vec3(1.0 / 2.2)); }

    // ACES filmic curve (Narkowicz 2015 fit): natural roll-off of highlights
    // instead of clipping sunlit summits to white.
    vec3 acesFilm(vec3 x) {
        return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0);
    }

    // Small value noise to break uniform land-cover patches.
    float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
    float valueNoise(vec2 p) {
        vec2 i = floor(p);
        vec2 f = fract(p);
        vec2 u = f * f * (3.0 - 2.0 * f);
        return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x),
                   mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);
    }

    // Mountain land cover (linear albedo) from four user colours: forest
    // below the tree line, alpine grass / bare soil above it, rock on slopes
    // steeper than uRockSlope (±6° transition), snow above the snow line on
    // slopes gentle enough to hold it (a little lower on north faces).
    // Each colour is modulated by ±12 % of noise so patches do not look
    // flat. The ×1.3 brings these natural (dark) albedos to the brightness
    // of the colour-map modes under the same light.
    vec3 landCoverAlbedo(float h, vec3 trueNormal) {
        float slopeDeg = degrees(acos(clamp(trueNormal.z, 0.0, 1.0)));
        float n1 = valueNoise(vUv * 180.0);
        float n2 = valueNoise(vUv * 45.0);
        float jitter = (n1 - 0.5) * 0.04 + (n2 - 0.5) * 0.06;
        float shade = 0.88 + 0.24 * n1;

        vec3 forest = srgbToLinear(uForestColor) * shade;
        vec3 grass  = srgbToLinear(uGrassColor) * (0.88 + 0.24 * n2);
        vec3 rock   = srgbToLinear(uRockColor) * shade;
        vec3 snow   = srgbToLinear(uSnowColor);

        float alpine = smoothstep(uTreeLine - 0.04, uTreeLine + 0.04, h + jitter);
        vec3 albedo = mix(forest, grass, alpine);

        float rockiness = smoothstep(uRockSlope - 6.0, uRockSlope + 6.0, slopeDeg + jitter * 120.0);
        rockiness = max(rockiness, smoothstep(uSnowLine - 0.10, uSnowLine, h) * 0.6);
        albedo = mix(albedo, rock, rockiness);

        float northBias = clamp(trueNormal.y, -1.0, 1.0) * 0.04;   // +y = north-facing
        float snowCover = smoothstep(uSnowLine - 0.02, uSnowLine + 0.03, h + jitter + northBias)
                        * (1.0 - smoothstep(uRockSlope + 4.0, uRockSlope + 18.0, slopeDeg));
        return mix(albedo, snow, snowCover) * 1.3;
    }

    void main() {
        // ── Surface normal (shading mode) ──────────────────────────────
        vec3 normal;
        if (uShadingMode == 2) {
            // Faceted look: normal of the triangle itself, from the screen
            // derivatives of the model-space position (same space as the sun).
            normal = normalize(cross(dFdx(vModelPosition), dFdy(vModelPosition)));
            if (normal.z < 0.0) normal = -normal;
        } else if (uShadingMode == 1 && uRenderStyle == 1) {
            normal = normalize(vWorldNormal);
        } else {
            normal = heightNormal(vUv, uHeightRange);
        }

        vec3 baseColor = uUseVertexColors ? vColor : uSolidColor;

        // ── CLASSIC STYLE (unchanged lighting model) ────────────────────
        if (uRenderStyle == 0) {
            vec3 finalColor;
            if (uShadingMode == 1) {
                finalColor = baseColor * vGouraudLighting;
            } else {
                vec3 skyColor = vec3(0.55, 0.68, 0.85);
                vec3 groundColor = vec3(0.20, 0.17, 0.13);
                vec3 ambient = mix(groundColor, skyColor, normal.z * 0.5 + 0.5) * uAmbientIntensity;
                vec3 L0 = normalize(uSunDirection);
                float diffuseTerm = max(dot(normal, L0), 0.0);
                if (uMultidirectional) {
                    vec3 L1 = vec3(-L0.y, L0.x, L0.z);
                    vec3 L2 = vec3(-L0.x, -L0.y, L0.z);
                    vec3 L3 = vec3(L0.y, -L0.x, L0.z);
                    diffuseTerm = max(dot(normal, L0), 0.0) * 0.4 + max(dot(normal, L1), 0.0) * 0.2
                                + max(dot(normal, L2), 0.0) * 0.2 + max(dot(normal, L3), 0.0) * 0.2;
                }
                float roughness2 = uRoughness * uRoughness * 1.8;
                float orenNayar = 1.0 - 0.75 * (roughness2 / (roughness2 + 0.25));
                float slopeFactor = pow(clamp(normal.z, 0.0, 1.0), 1.0 + uSlopeContrast * 25.0);
                vec3 lighting = ambient * slopeFactor + uSunColor * diffuseTerm * orenNayar * uSunIntensity;
                finalColor = baseColor * lighting * mix(1.0, slopeFactor, uSlopeContrast * 0.75);
            }
            gl_FragColor = vec4(finalColor, 1.0);
            return;
        }

        // ── REALISTIC STYLE ─────────────────────────────────────────────
        float h = heightAt(vUv);
        vec3 albedo;
        if (uLandCover) {
            albedo = landCoverAlbedo(h, heightNormal(vUv, uRealHeightRange));
        } else {
            albedo = srgbToLinear(baseColor);
        }

        vec2 bake = texture2D(uShadowAO, vUv).rg;
        float sunVisibility = uCastShadows ? bake.r : 1.0;
        float skyView = mix(1.0, bake.g, uAOStrength);

        vec3 L0 = normalize(uSunDirection);
        float direct = max(dot(normal, L0), 0.0);
        if (uMultidirectional) {
            // GIS multidirectional option kept in the realistic style: the
            // three extra lights only fill shaded slopes (no cast shadow).
            vec3 L1 = vec3(-L0.y, L0.x, L0.z);
            vec3 L2 = vec3(-L0.x, -L0.y, L0.z);
            vec3 L3 = vec3(L0.y, -L0.x, L0.z);
            direct = direct * 0.55 * sunVisibility
                   + (max(dot(normal, L1), 0.0) + max(dot(normal, L2), 0.0)
                      + max(dot(normal, L3), 0.0)) * 0.15;
        } else {
            direct *= sunVisibility;
        }
        float roughness2 = uRoughness * uRoughness * 1.8;
        float orenNayar = 1.0 - 0.5 * (roughness2 / (roughness2 + 0.25));

        // Irradiance balance: with the default sun (intensity 1.5, 45°) and
        // ambient (0.3), flat open ground receives ~25 % of its light from
        // the sky, the usual clear-day ratio.
        vec3 sunLight = srgbToLinear(uSunColor) * uSunIntensity * direct * orenNayar;
        // Sky light: strongest on surfaces facing up, reduced by the
        // portion of sky hidden by the surrounding relief.
        vec3 skyLight = srgbToLinear(uSkyTint) * uAmbientIntensity * 1.1
                      * (0.55 + 0.45 * normal.z) * skyView;
        // Light reflected by the opposite valley side (warm, weak).
        vec3 bounce = vec3(0.30, 0.25, 0.20) * uAmbientIntensity * 0.35
                    * (1.0 - normal.z) * skyView;

        // Optional slope accentuation, milder than in the classic style.
        float slopeFactor = mix(1.0, pow(clamp(normal.z, 0.0, 1.0), 1.0 + uSlopeContrast * 6.0),
                                uSlopeContrast * 0.5);
        vec3 color = albedo * (sunLight + skyLight + bounce) * slopeFactor;

        // Aerial perspective: exponential haze with view distance (the
        // model is ~100 units wide, so 0.004/unit gives ~35 % haze across
        // the scene at full strength).
        float viewDistance = length(vViewPosition);
        float haze = 1.0 - exp(-viewDistance * 0.004 * uAtmosphere);
        color = mix(color, srgbToLinear(uHazeColor) * 1.1, haze);

        // Calibration: exposure 1.0 (slider 100 %) renders open, sunlit
        // ground with the mean brightness of the classic hillshade while
        // keeping shaded slopes readable (measured on the Sierra Nevada
        // test scene). Without the 0.5 factor the realistic style was about
        // twice as bright as the classic one.
        color = acesFilm(color * uExposure * 0.5);
        gl_FragColor = vec4(linearToSrgb(color), 1.0);
    }
`;


// ---------------------------------------------------------------------------
// 1b. LIGHTING BAKE (cast shadows + sky-view factor → uShadowAO texture)
// ---------------------------------------------------------------------------
//
// Rendered once into a texture the size of the height texture, then only
// when the sun, the vertical exaggeration or the softness change. Per texel:
//
//   r — sun visibility: a ray is marched from the surface towards the sun
//       through the height field; the soft penumbra keeps the smallest
//       ratio (clearance / distance) met along the ray (Quilez' soft
//       shadows). 1 = lit, 0 = in the shadow of the relief.
//   g — sky-view factor (Zakšek et al., 2011): mean over 16 azimuths of
//       1 − sin(horizon elevation angle), the horizon searched up to ~60
//       texels away. It darkens valley floors and gorges the way diffuse
//       skylight does, and is a published relief-visualisation index.

const bakeVertexShader = `
    varying vec2 vUv;
    void main() {
        vUv = uv;
        gl_Position = vec4(position.xy, 0.0, 1.0);
    }
`;

const bakeFragmentShader = `
    uniform sampler2D uHeightmap;
    uniform vec2 uTextureSize;
    uniform vec2 uModelSize;
    uniform float uHeightRange;
    uniform vec3 uSunDirection;
    uniform float uShadowSoftness;    // 0 sharp … 1 very soft

    varying vec2 vUv;

    float heightAt(vec2 uv) { return texture2D(uHeightmap, uv).r * uHeightRange; }

    float sunVisibility(vec2 uv, float z0, vec3 L) {
        if (L.z <= 0.0) return 0.0;                       // sun below the horizon
        float texel = max(uModelSize.x / uTextureSize.x, uModelSize.y / uTextureSize.y);
        float k = mix(48.0, 4.0, uShadowSoftness);        // penumbra sharpness
        float t = texel;
        float visibility = 1.0;
        vec3 origin = vec3(uv * uModelSize, z0 + texel * 0.25);
        for (int i = 0; i < 160; i++) {
            vec3 p = origin + L * t;
            vec2 q = p.xy / uModelSize;
            if (q.x < 0.0 || q.x > 1.0 || q.y < 0.0 || q.y > 1.0) break;
            if (p.z > uHeightRange * 1.05) break;         // above every summit
            float clearance = p.z - heightAt(q);
            visibility = min(visibility, k * clearance / t);
            if (visibility <= 0.0) break;
            t += max(texel * 0.75, t * 0.04);
        }
        return smoothstep(0.0, 1.0, clamp(visibility, 0.0, 1.0));
    }

    float skyViewFactor(vec2 uv, float z0) {
        vec2 texel = 1.0 / uTextureSize;
        float texelModel = max(uModelSize.x / uTextureSize.x, uModelSize.y / uTextureSize.y);
        float sum = 0.0;
        for (int d = 0; d < 16; d++) {
            float a = float(d) * 0.392699;                // 2π / 16
            vec2 dir = vec2(cos(a), sin(a));
            float maxTan = 0.0;
            float r = 1.5;
            for (int s = 0; s < 10; s++) {
                vec2 q = uv + dir * r * texel;
                if (q.x < 0.0 || q.x > 1.0 || q.y < 0.0 || q.y > 1.0) break;
                float dz = heightAt(q) - z0;
                maxTan = max(maxTan, dz / (r * texelModel));
                r *= 1.55;
            }
            sum += 1.0 - maxTan / sqrt(1.0 + maxTan * maxTan);   // 1 − sin(horizon angle)
        }
        return sum / 16.0;
    }

    void main() {
        float z0 = heightAt(vUv);
        gl_FragColor = vec4(sunVisibility(vUv, z0, normalize(uSunDirection)),
                            skyViewFactor(vUv, z0), 0.0, 1.0);
    }
`;

let bakeState = null;          // {target, scene, camera, material}
let bakeDirty = false;         // re-bake requested (done once per frame at most)

/** Create (or resize) the render target and full-screen pass of the bake. */
function _setupLightingBake(heightTexture, texWidth, texHeight) {
    if (bakeState) {
        bakeState.target.dispose();
        bakeState.material.dispose();
    }
    const target = new THREE.WebGLRenderTarget(texWidth, texHeight, {
        minFilter: THREE.LinearFilter,
        magFilter: THREE.LinearFilter,
        format: THREE.RGBAFormat,
        type: THREE.UnsignedByteType,
        depthBuffer: false,
        stencilBuffer: false,
    });
    const material = new THREE.ShaderMaterial({
        vertexShader: bakeVertexShader,
        fragmentShader: bakeFragmentShader,
        uniforms: {
            uHeightmap: { value: heightTexture },
            uTextureSize: { value: new THREE.Vector2(texWidth, texHeight) },
            uModelSize: { value: new THREE.Vector2(1, 1) },
            uHeightRange: { value: 1.0 },
            uSunDirection: { value: sunDirection.clone() },
            uShadowSoftness: { value: realism.shadowSoftness },
        },
        depthTest: false,
        depthWrite: false,
    });
    const quadScene = new THREE.Scene();
    quadScene.add(new THREE.Mesh(new THREE.PlaneGeometry(2, 2), material));
    bakeState = { target, scene: quadScene, camera: new THREE.Camera(), material };
    bakeDirty = true;
}

/** Render the bake now if something it depends on changed. */
function _bakeLightingIfNeeded() {
    if (!bakeDirty || !bakeState || !renderer) return;
    const terrainObj = _findTerrainObject();
    if (!terrainObj) return;
    const t = terrainObj.mesh.material.uniforms;
    const u = bakeState.material.uniforms;
    u.uModelSize.value.copy(t.uModelSize.value);
    u.uHeightRange.value = t.uHeightRange.value;
    u.uSunDirection.value.copy(sunDirection);
    u.uShadowSoftness.value = realism.shadowSoftness;
    const previous = renderer.getRenderTarget();
    renderer.setRenderTarget(bakeState.target);
    renderer.render(bakeState.scene, bakeState.camera);
    renderer.setRenderTarget(previous);
    t.uShadowAO.value = bakeState.target.texture;
    bakeDirty = false;
}

/**
 * Realistic-rendering settings (driven by the "Realism" controls of the
 * panel through the "update_realism" command).
 */
const realism = {
    style: 1,               // 1 realistic, 0 classic hillshade
    castShadows: true,
    shadowSoftness: 0.35,
    aoStrength: 0.85,
    atmosphere: 0.25,
    exposure: 1.0,
    treeLineM: null,        // null → 55 % of the elevation range
    snowLineM: null,        // null → 85 % of the elevation range
    rockSlopeDeg: 35.0,     // bare rock above this slope
    landColors: {           // sRGB, as shown in the colour buttons
        forest: "#3f5034",
        grass: "#8a8460",
        rock: "#7a6f63",
        snow: "#eef1f5",
    },
};

/** Normalised (0–1) tree and snow lines for the current DEM. */
function _landCoverLines() {
    const range = Math.max(1e-6, elevationStats.max - elevationStats.min);
    const tree = realism.treeLineM === null ? 0.55 : (realism.treeLineM - elevationStats.min) / range;
    const snow = realism.snowLineM === null ? 0.85 : (realism.snowLineM - elevationStats.min) / range;
    return { tree, snow };
}

function createCustomMaterial(useVertexColors, solidColorObj, heightTexture, width, height) {
    const lines = _landCoverLines();
    return new THREE.ShaderMaterial({
        vertexShader: terrainVertexShader,
        fragmentShader: terrainFragmentShader,
        uniforms: {
            uSunDirection: { value: sunDirection },
            uSunColor: { value: sunColor },
            uSunIntensity: { value: sunIntensity },
            uAmbientColor: { value: ambientColor },
            uAmbientIntensity: { value: ambientIntensity },
            uSolidColor: { value: solidColorObj || new THREE.Color(0xffffff) },
            uUseVertexColors: { value: useVertexColors },
            uRoughness: { value: globalRoughness },
            uSlopeContrast: { value: globalSlopeContrast },
            uMultidirectional: { value: globalMultidirectional },
            uShadingMode: { value: currentShadingMode },
            uHeightmap: { value: heightTexture || null },
            uTextureSize: { value: new THREE.Vector2(width || 100, height || 100) },
            uZScale: { value: currentZScale },
            uRenderStyle: { value: realism.style },
            uModelSize: { value: new THREE.Vector2(100, 100) },
            uHeightRange: { value: 1.0 },
            uRealHeightRange: { value: 1.0 },
            uShadowAO: { value: null },
            uCastShadows: { value: realism.castShadows },
            uAOStrength: { value: realism.aoStrength },
            uAtmosphere: { value: realism.atmosphere },
            uExposure: { value: realism.exposure },
            uSkyTint: { value: new THREE.Color(0.60, 0.72, 0.90) },
            uHazeColor: { value: new THREE.Color(0.72, 0.79, 0.88) },
            uLandCover: { value: colorMode === "natural" },
            uTreeLine: { value: lines.tree },
            uSnowLine: { value: lines.snow },
            uRockSlope: { value: realism.rockSlopeDeg },
            uForestColor: { value: new THREE.Color(realism.landColors.forest) },
            uGrassColor: { value: new THREE.Color(realism.landColors.grass) },
            uRockColor: { value: new THREE.Color(realism.landColors.rock) },
            uSnowColor: { value: new THREE.Color(realism.landColors.snow) },
        },
        side: THREE.DoubleSide,
        extensions: { derivatives: true },
    });
}

/** Push the realism settings into the terrain and structure materials. */
function _applyRealismUniforms() {
    const lines = _landCoverLines();
    for (const id in sceneObjects) {
        const obj = sceneObjects[id];
        const uniforms = obj && obj.mesh && obj.mesh.material && obj.mesh.material.uniforms;
        if (!uniforms) continue;
        if (uniforms.uRenderStyle) uniforms.uRenderStyle.value = realism.style;
        if (uniforms.uCastShadows) uniforms.uCastShadows.value = realism.castShadows;
        if (uniforms.uAOStrength) uniforms.uAOStrength.value = realism.aoStrength;
        if (uniforms.uAtmosphere) uniforms.uAtmosphere.value = realism.atmosphere;
        if (uniforms.uExposure) uniforms.uExposure.value = realism.exposure;
        if (uniforms.uLandCover) uniforms.uLandCover.value = colorMode === "natural";
        if (uniforms.uTreeLine) uniforms.uTreeLine.value = lines.tree;
        if (uniforms.uSnowLine) uniforms.uSnowLine.value = lines.snow;
        if (uniforms.uRockSlope) uniforms.uRockSlope.value = realism.rockSlopeDeg;
        if (uniforms.uForestColor) {
            uniforms.uForestColor.value.set(realism.landColors.forest);
            uniforms.uGrassColor.value.set(realism.landColors.grass);
            uniforms.uRockColor.value.set(realism.landColors.rock);
            uniforms.uSnowColor.value.set(realism.landColors.snow);
        }
    }
    bakeDirty = true;
}

// ---------------------------------------------------------------------------
// 2. STRUCTURAL SHADERS (No Color Attributes, No WebGL Mismatches)
// ---------------------------------------------------------------------------

const structuralVertexShader = `
    varying vec3 vNormal;
    varying vec3 vWorldNormal;

    void main() {
        vNormal = normalize(normalMatrix * normal);
        vWorldNormal = normalize(normal);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }
`;

const structuralFragmentShader = `
    uniform vec3 uSunDirection;
    uniform vec3 uSunColor;
    uniform float uSunIntensity;
    uniform float uAmbientIntensity;
    uniform vec3 uSolidColor;
    uniform int uRenderStyle;   // 1: same linear lighting + ACES as the realistic terrain
    uniform float uExposure;

    varying vec3 vNormal;
    varying vec3 vWorldNormal;

    vec3 acesFilm(vec3 x) {
        return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0);
    }

    void main() {
        vec3 normal = normalize(vNormal);
        vec3 worldNormal = normalize(vWorldNormal);

        vec3 L = normalize(uSunDirection);
        float NdotL = max(dot(normal, L), 0.0);

        // Pure hemispherical ambient fill using static world coordinates (Up is Z)
        vec3 skyColor = vec3(0.65, 0.75, 0.90); // Brightened sky reflection
        vec3 groundColor = vec3(0.35, 0.30, 0.25); // Warm ground bounce
        float hemiMix = worldNormal.z * 0.5 + 0.5;

        // Boost ambient intensity specifically for vertical walls (+0.25 flat offset)
        vec3 ambient = mix(groundColor, skyColor, hemiMix) * (uAmbientIntensity + 0.25);

        vec3 finalLighting = ambient + (uSunColor * NdotL * uSunIntensity);
        if (uRenderStyle == 1) {
            vec3 linear = pow(uSolidColor, vec3(2.2)) * finalLighting * 0.7 * uExposure;
            gl_FragColor = vec4(pow(acesFilm(linear), vec3(1.0 / 2.2)), 1.0);
        } else {
            gl_FragColor = vec4(uSolidColor * finalLighting, 1.0);
        }
    }
`;

function createStructuralMaterial(solidColorObj) {
    return new THREE.ShaderMaterial({
        vertexShader: structuralVertexShader,
        fragmentShader: structuralFragmentShader,
        uniforms: {
            uSunDirection: { value: sunDirection },
            uSunColor: { value: sunColor },
            uSunIntensity: { value: sunIntensity },
            uAmbientIntensity: { value: ambientIntensity },
            uSolidColor: { value: solidColorObj || new THREE.Color(0xffffff) },
            uRenderStyle: { value: realism.style },
            uExposure: { value: realism.exposure }
        },
        side: THREE.DoubleSide
    });
}

// Global uniform updater
function updateSceneUniforms() {
    for (const id in sceneObjects) {
        const obj = sceneObjects[id];
        if (obj && obj.mesh && obj.mesh.material && obj.mesh.material.uniforms) {
            const uniforms = obj.mesh.material.uniforms;
            if (uniforms.uSunDirection) uniforms.uSunDirection.value.copy(sunDirection);
            if (uniforms.uSunColor) uniforms.uSunColor.value.copy(sunColor);
            if (uniforms.uSunIntensity) uniforms.uSunIntensity.value = sunIntensity;
            if (uniforms.uAmbientColor) uniforms.uAmbientColor.value.copy(ambientColor);
            if (uniforms.uAmbientIntensity) uniforms.uAmbientIntensity.value = ambientIntensity;
            if (uniforms.uRoughness) uniforms.uRoughness.value = globalRoughness;
            if (uniforms.uSlopeContrast) uniforms.uSlopeContrast.value = globalSlopeContrast;
            if (uniforms.uMultidirectional) uniforms.uMultidirectional.value = globalMultidirectional;
        }
    }
    // Cast shadows depend on the sun direction: re-bake on the next frame.
    bakeDirty = true;
}


// ---------------------------------------------------------------------------
// Color utility: Z index of a logical vertex in a flat XYZ buffer
// ---------------------------------------------------------------------------

function zOffsetOf(vertexIndex) {
    return vertexIndex * 3 + 2;
}

// ---------------------------------------------------------------------------
// Scene registration helpers
// ---------------------------------------------------------------------------

function registerObject(elementId, mesh, type) {
    sceneObjects[elementId] = { mesh, type, visible: true };
}

function unregisterObject(elementId) {
    const obj = sceneObjects[elementId];
    if (obj) {
        scene.remove(obj.mesh);

        // Recursively dispose geometries and materials
        if (obj.mesh.geometry) {
            obj.mesh.geometry.dispose();
        }
        if (obj.mesh.material) {
            if (Array.isArray(obj.mesh.material)) {
                obj.mesh.material.forEach(mat => mat.dispose());
            } else {
                obj.mesh.material.dispose();
            }
        }
        delete sceneObjects[elementId];
    }
}

// ---------------------------------------------------------------------------
// Scene initialization
// ---------------------------------------------------------------------------
function initScene() {
    const container = document.getElementById("viewport-container");

    // Scene
    scene = new THREE.Scene();

    const aspect = container.clientWidth / container.clientHeight;

    // 1. Perspective Camera setup
    perspectiveCamera = new THREE.PerspectiveCamera(45, aspect, 0.1, 1000.0);
    perspectiveCamera.position.set(0, -150, 120);

    // 2. Orthographic Camera setup (frustum sized to comfortably fit our 100-unit models)
    const frustumSize = 120.0;
    orthographicCamera = new THREE.OrthographicCamera(
        (frustumSize * aspect) / -2,
        (frustumSize * aspect) / 2,
        frustumSize / 2,
        (frustumSize) / -2,
        0.1,
        1000.0
    );
    orthographicCamera.position.copy(perspectiveCamera.position);

    // Default camera at start
    camera = perspectiveCamera;

    // Renderer — configured with antialiasing and transparency (alpha channel)
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setSize(container.clientWidth, container.clientHeight);
    renderer.autoClear = false; // Required for multi-viewport corner rendering
    container.appendChild(renderer.domElement);

    // Orbit controls
    controls = new THREE.TrackballControls(camera, renderer.domElement);

    controls.rotateSpeed = 2.1;
    controls.zoomSpeed = 0.5;
    controls.panSpeed = 0.15;

    controls.staticMoving = false;
    controls.enableDamping = true;
    controls.dynamicDampingFactor = 0.15;

    controls.minDistance = 50;
    controls.maxDistance = 10000000;

    controls.mouseButtons = {
        LEFT: THREE.MOUSE.ROTATE,    // Left-click drag to rotate (orbit)
        MIDDLE: THREE.MOUSE.PAN,     // Middle-click drag to pan (move flat)
        RIGHT: THREE.MOUSE.DOLLY     // Right-click drag to zoom (dolly)
    };

    // Apply the sky gradient now that both 'scene' and 'renderer' are fully initialized
    _applySkyGradient();

    // Create a clean camera status overlay dynamically at the top-right
    _createCameraOverlay(container);

    // Default scene lights
    _addDefaultLights();

    // Start loop
    _animate();

    window.addEventListener("resize", _onWindowResize);
}

function _onWindowResize() {
    const container = document.getElementById("viewport-container");
    const aspect = container.clientWidth / container.clientHeight;

    // Rescale Perspective Projection
    perspectiveCamera.aspect = aspect;
    perspectiveCamera.updateProjectionMatrix();

    // Rescale Orthographic Projection bounds
    const frustumSize = 120.0;
    orthographicCamera.left = (frustumSize * aspect) / -2;
    orthographicCamera.right = (frustumSize * aspect) / 2;
    orthographicCamera.top = frustumSize / 2;
    orthographicCamera.bottom = (frustumSize) / -2;
    orthographicCamera.updateProjectionMatrix();

    renderer.setSize(container.clientWidth, container.clientHeight);
}

function _animate() {
    requestAnimationFrame(_animate);
    controls.update();
    _bakeLightingIfNeeded();

    const container = document.getElementById("viewport-container");
    const width = container.clientWidth;
    const height = container.clientHeight;

    // 1. Render Primary 3D Terrain Viewport
    renderer.setViewport(0, 0, width, height);
    renderer.setScissor(0, 0, width, height);
    renderer.setScissorTest(false);
    renderer.clear();
    renderer.render(scene, camera);

    // 2. Render Corner Orientation Marker Widget (Bottom-Right)
    if (axesSceneVisible && axesScene && axesCamera) {
        const axesSize = 110; // 110x110 px fixed overlay box
        const padding = 10;
        const left = width - axesSize - padding;
        const bottom = padding;

        renderer.setViewport(left, bottom, axesSize, axesSize);
        renderer.setScissor(left, bottom, axesSize, axesSize);
        renderer.setScissorTest(true);

        // Extract camera rotational vector without inheriting translation or zoom
        axesCamera.position.copy(camera.position).sub(controls.target).setLength(25);
        axesCamera.up.copy(camera.up);
        axesCamera.lookAt(0, 0, 0);

        renderer.clearDepth(); // Prevent underlying terrain from clipping the axes
        renderer.render(axesScene, axesCamera);

        // Restore default scissor state
        renderer.setScissorTest(false);
        renderer.setViewport(0, 0, width, height);
    }
}



// ---------------------------------------------------------------------------
// Default lights
// ---------------------------------------------------------------------------

function _addDefaultLights() {
    // We add actual THREE lights purely as dummy tracking objects for UI/helpers
    const ambient = new THREE.AmbientLight(0xffffff, 0.3);
    ambient.name = "ambient_default";
    scene.add(ambient);

    const sun = new THREE.DirectionalLight(0xffffff, 1.5);
    sun.name = "light_0";
    const sunPos = _azimuthAltitudeToXYZ(135, 45, 150);
    sun.position.set(sunPos.x, sunPos.y, sunPos.z);
    scene.add(sun);
    scene.add(sun.target); // Ensures matrix updates correctly
    sceneObjects["light_0"] = { mesh: sun, type: "light", visible: true };

    const hemi = new THREE.HemisphereLight(0xffffff, 0x444444, 0.3);
    hemi.position.set(0, 0, 120);
    scene.add(hemi);

    // Initial shader uniforms synchronization
    sunIntensity = 1.5;
    sunColor.set(0xffffff);
    sunDirection.set(sunPos.x, sunPos.y, sunPos.z).normalize(); // Safe initialization from plain JS object
    ambientIntensity = 0.3;
    ambientColor.set(0xffffff);

    // ── Permanent Scene Floor Grid (Remains in 3D world space) ──────────
    const gridHelper = new THREE.GridHelper(200, 20, 0x555555, 0x2d2d2d);
    gridHelper.rotation.x = Math.PI / 2;
    gridHelper.position.set(0, 0, -10);
    scene.add(gridHelper);
    registerObject("scene_grid", gridHelper, "helper");

    // Initialize Corner Orientation Marker Widget
    _initCornerAxes();
}

function _initCornerAxes() {
    axesScene = new THREE.Scene();

    // Orthographic projection prevents perspective distortion during rotation
    const frustum = 18;
    axesCamera = new THREE.OrthographicCamera(
        -frustum / 2, frustum / 2,
        frustum / 2, -frustum / 2,
        0.1, 100
    );
    axesCamera.position.set(0, -25, 25);
    axesCamera.lookAt(0, 0, 0);

    const ambient = new THREE.AmbientLight(0xffffff, 1.0);
    axesScene.add(ambient);

    const axesHelper = new THREE.AxesHelper(7);
    axesScene.add(axesHelper);

    const xLabel = _createLabelSprite("X", "#ff4444");
    xLabel.position.set(8.5, 0, 0);
    axesScene.add(xLabel);

    const yLabel = _createLabelSprite("Y", "#44ff44");
    yLabel.position.set(0, 8.5, 0);
    axesScene.add(yLabel);

    const zLabel = _createLabelSprite("Z", "#4444ff");
    zLabel.position.set(0, 0, 8.5);
    axesScene.add(zLabel);
}

function _createLabelSprite(text, color) {
    const canvas = document.createElement("canvas");
    canvas.width = 64;
    canvas.height = 64;
    const ctx = canvas.getContext("2d");
    ctx.font = "Bold 44px sans-serif";
    ctx.fillStyle = color;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, 32, 32);

    const texture = new THREE.CanvasTexture(canvas);
    const material = new THREE.SpriteMaterial({ map: texture, transparent: true });
    const sprite = new THREE.Sprite(material);
    sprite.scale.set(3.5, 3.5, 1);
    return sprite;
}
// ---------------------------------------------------------------------------
// ACTION DISPATCHER
// ---------------------------------------------------------------------------

function processPythonCommand(command) {
    if (!command || !command.action) return;
    // Log the action only: payloads (DEM grids) can weigh several megabytes.
    console.log("[JS Receive Command] " + command.action);
    try {
        switch (command.action) {

            // ── Terrain ──────────────────────────────────────────────────
            case "set_main_raster":
                _buildTerrain(command.payload);
                break;

            case "set_camera_projection":
                _setCameraProjection(command.payload.mode);
                break;

            // ── Z-scale ──────────────────────────────────────────────────
            case "update_z_scale":
                _updateZScale(command.payload.scale);
                break;

            // ── Block base thickness ─────────────────────────────────────
            case "update_base_thickness":
                baseThicknessFraction = command.payload.thickness;
                if (originalDEMValues) _rebuildBlockBase();
                break;

            // ── Visibility ───────────────────────────────────────────────
            case "toggle_visibility": {
                const elementId = command.payload.element_id;
                const isVisible = command.payload.visible;
                if (elementId === "scene_axes") {
                    axesSceneVisible = isVisible;
                    break;
                }
                // Handle the dynamic Legend overlay visibility toggle [New]
                if (elementId === "scene_legend") {
                    legendVisibleUserPref = isVisible;
                    const legend = document.getElementById("legend-overlay");
                    if (legend) {
                        legend.style.display = isVisible ? "block" : "none";
                        // Force a redraw to synchronize state
                        _updateLegendWidget();
                    }
                    break;
                }

                if (elementId === "block_base") {
                    blockBaseVisible = isVisible; // Cache user preference globally
                    ["block_base_walls", "block_base_sole"].forEach(subId => {
                        const obj = sceneObjects[subId];
                        if (obj) {
                            obj.mesh.visible = isVisible;
                            obj.visible = isVisible;
                        }
                    });
                } else {
                    // Match and toggle visibility for all sub-features under this layer prefix [Visibility Fix]
                    for (const id in sceneObjects) {
                        if (id.startsWith(elementId)) {
                            sceneObjects[id].mesh.visible = isVisible;
                            sceneObjects[id].visible = isVisible;
                        }
                    }
                }
                break;
            }
            case "update_vector_style":
                _applyDynamicVectorStyle(command.payload);
                break;
            
            case "remove_vector_layer": {
                const elementId = command.payload.element_id;

                // Identify and cleanly dispose of all meshes starting with this layer prefix [Deletion Fix]
                const keysToRemove = [];
                for (const id in sceneObjects) {
                    if (id.startsWith(elementId)) {
                        keysToRemove.push(id);
                    }
                }

                keysToRemove.forEach(key => {
                    unregisterObject(key);
                });
                break;
            }
                
            case "set_multidirectional_shading":
                globalMultidirectional = command.payload.enabled;
                updateSceneUniforms();
                break;
                
            // ── Aesthetic / Shading Controls ─────────────────────────────
            case "update_aesthetic_shading":
                globalRoughness = command.payload.roughness;
                globalSlopeContrast = command.payload.slope_contrast;
                updateSceneUniforms();
                break;

            // ── Colormaps ────────────────────────────────────────────────
            // Inside processPythonCommand:

            case "set_classified_colors":
                colorMode = "classified";
                currentClassColors = command.payload.colors;
                currentClassBounds = command.payload.bounds;
                _applyClassifiedColors();
                break;

            case "set_colormap":
                colorMode = "colormap"; // Register active mode
                currentColormapName = command.payload.name;
                currentColormapReverse = command.payload.reverse || false;
                _applyColormap();
                break;

            case "set_solid_color":
                colorMode = "solid"; // Register active mode
                _applySolidColor(command.payload.color);
                break;

            case "set_color_bounds":
                colorBoundsAuto = false;
                colorBoundsMin = command.payload.min_z;
                colorBoundsMax = command.payload.max_z;
                _applyColormap();
                break;
            
            case "set_color_bounds_auto": 
                colorBoundsAuto = true;
                _applyColormap();
                break;

            // ── Block colors ─────────────────────────────────────────────
            case "set_walls_color":
                wallsColor = new THREE.Color(command.payload.color); 
                _setObjectColor("block_base_walls", command.payload.color);
                break;

            case "set_base_color":
                baseColor = new THREE.Color(command.payload.color);  
                _setObjectColor("block_base_sole", command.payload.color);
                break;

            // ── Sky colors ───────────────────────────────────────────────
            case "set_sky_top_color":
                skyColorTop = new THREE.Color(command.payload.color);
                _applySkyGradient();
                break;

            case "set_sky_bottom_color":
                skyColorBottom = new THREE.Color(command.payload.color);
                _applySkyGradient();
                break;

            // ── Lights ───────────────────────────────────────────────────
            case "add_light":
                _addLight(command.payload);
                break;

            case "update_light":
                _updateLight(command.payload);
                break;

            case "remove_light":
                unregisterObject(command.payload.id);
                break;

            // ── Shading (legacy, kept for compatibility) ─────────────────
            case "update_realism": {
                const p = command.payload;
                if (p.style !== undefined) realism.style = p.style === "classic" ? 0 : 1;
                if (p.shadows !== undefined) realism.castShadows = !!p.shadows;
                if (p.shadow_softness !== undefined) realism.shadowSoftness = p.shadow_softness;
                if (p.ao !== undefined) realism.aoStrength = p.ao;
                if (p.atmosphere !== undefined) realism.atmosphere = p.atmosphere;
                if (p.exposure !== undefined) realism.exposure = p.exposure;
                _applyRealismUniforms();
                break;
            }

            case "set_natural_colors":
                colorMode = "natural";
                _applyNaturalColors(command.payload);
                break;

            case "set_shading_mode":
                _updateShadingMode(command.payload.mode);
                break;

            case "export_viewport":
                _exportViewport(command.payload.format, command.payload.dpi);
                break;
            case "export_stl":
                _exportSTL();
                break;
            
           case "add_vector_batch":
                if (Array.isArray(command.payload)) {
                    command.payload.forEach(featureDescriptor => {
                        _buildVectorFeature(featureDescriptor);
                    });
                }
                break;

            default:
                console.warn("[RockMorph 3D] Unknown action:", command.action);
        }
    } catch (e) {
        console.error("[RockMorph 3D] Error in action:", command.action, e);
    }
}

// ---------------------------------------------------------------------------
// TERRAIN BUILD
// ---------------------------------------------------------------------------

/**
 * Float height texture normalised to [0, 1] between z_min and z_max.
 * Rows are written south to north (texture v grows northwards, like the
 * plane's uv). NoData becomes the lowest elevation so that it neither casts
 * shadows nor blocks the sky.
 */
function _buildHeightTexture(data) {
    const grid = (data.texture_values && data.texture_width > 1) ? data.texture_values : data.z_values;
    const texHeight = grid.length;
    const texWidth = grid[0].length;
    const range = (data.z_max - data.z_min) || 1.0;
    // WebGL 2 has one-channel float textures; WebGL 1 (older GPUs/drivers,
    // where QtWebEngine falls back) only has RGBA float textures
    // (OES_texture_float). A RedFormat texture there reads as zero, which
    // flattened the shading.
    const webgl2 = !!(renderer && renderer.capabilities.isWebGL2);
    const channels = webgl2 ? 1 : 4;
    const values = new Float32Array(texWidth * texHeight * channels);
    for (let j = 0; j < texHeight; j++) {
        const row = grid[j];
        const offset = (texHeight - 1 - j) * texWidth;
        for (let i = 0; i < texWidth; i++) {
            const z = row[i];
            values[(offset + i) * channels] = (z === null || z === undefined || isNaN(z) || z === data.nodata_value)
                ? 0.0 : (z - data.z_min) / range;
        }
    }
    const texture = new THREE.DataTexture(values, texWidth, texHeight,
        webgl2 ? THREE.RedFormat : THREE.RGBAFormat, THREE.FloatType);
    texture.minFilter = THREE.LinearFilter;
    texture.magFilter = THREE.LinearFilter;
    texture.wrapS = texture.wrapT = THREE.ClampToEdgeWrapping;
    texture.needsUpdate = true;
    return { texture, width: texWidth, height: texHeight };
}

function _buildTerrain(data) {
    // console.log("[RockMorph 3D] Building terrain:", data.element_id,
    //     "— size:", data.width, "×", data.height,
    //     "— Z:", data.z_min, "→", data.z_max);
    
    // Determine the active color ramp using fallback cascading
    const activeRamp = (typeof ACTIVE_SCENE_DATA !== "undefined" && ACTIVE_SCENE_DATA.style && ACTIVE_SCENE_DATA.style.active_ramp && ACTIVE_SCENE_DATA.style.active_ramp.length > 0)
        ? ACTIVE_SCENE_DATA.style.active_ramp
        : (COLORMAPS[currentColormapName] || COLORMAPS.viridis || null);

    // 1. Sweep and fully dispose of all old terrain, block, and vector layers [Vector Persistence Fix]
    const keysToRemove = [];
    for (const id in sceneObjects) {
        const type = sceneObjects[id].type;
        // Purge all spatial meshes, keeping only active lights and their helpers
        if (type !== "light" && type !== "gizmo" && id !=="scene_grid") {
            keysToRemove.push(id);
        }
    }
    keysToRemove.forEach(key => {
        unregisterObject(key);
    });

    // 2. Reset base reference variables
    wallMesh = null;
    wallVertexMappings = [];

    // Cache for Z-scale and colormap updates
    originalDEMValues = data.z_values;
    elevationStats.min = data.z_min;
    elevationStats.max = data.z_max;
    spatialOffsets.z = data.z_min;

    spatialOffsets.x = (data.x_coords[0] + data.x_coords[data.x_coords.length - 1]) / 2;
    spatialOffsets.y = (data.y_coords[0] + data.y_coords[data.y_coords.length - 1]) / 2;

    const width = data.width;
    const height = data.height;

    const modelWidth = Math.abs(data.x_coords[data.x_coords.length - 1] - data.x_coords[0]);
    const modelHeight = Math.abs(data.y_coords[data.y_coords.length - 1] - data.y_coords[0]);
    cachedModelWidth = modelWidth;
    cachedModelHeight = modelHeight;
    const maxDim = Math.max(modelWidth, modelHeight);
    
    // SCALE DOWN : Normalizes coords to a maximum size of 100 units to prevent shader float underflow
    scaleFactor = 100.0 / maxDim;
    modelMaxDim = 100.0; 

    const elevRange = Math.max(1.0, data.z_max - data.z_min);

    // baseExaggeration: 1.0 slider unit = 15% of max dimension in Z
    baseExaggeration = (maxDim * 0.15) / elevRange;

    // ── Geometry scaled ──────────────────────────────────────────────────
    const geometry = new THREE.PlaneGeometry(
        modelWidth * scaleFactor,
        modelHeight * scaleFactor,
        width - 1, height - 1
    );

    const positions = geometry.attributes.position.array;
    // ── Generate High-Fidelity Float32 Heightmap Texture for GPU Shading ──

    const colors = [];
    const indices = [];

    const isNoData = (r, c) => {
        const v = data.z_values[r][c];
        return v === null || v === undefined || isNaN(v) || v === data.nodata_value;
    };

    let vi = 0;
    for (let j = 0; j < height; j++) {
        for (let i = 0; i < width; i++) {
            const rawZ = data.z_values[j][i];
            let z = 0;

            if (rawZ !== null && rawZ !== undefined && !isNaN(rawZ) && rawZ !== data.nodata_value) {
                z = (rawZ - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;
            }
            positions[vi + 2] = z;
            vi += 3;

            // Vertex color from current colormap
            const t = _normZ(rawZ ?? data.z_min, data);
            const c = _sampleRamp(activeRamp, currentColormapReverse ? 1 - t : t);
            colors.push(c.r, c.g, c.b);


            // Build index buffer, skip NoData quads
            if (j < height - 1 && i < width - 1) {
                const vTL = j * width + i;
                const vTR = j * width + (i + 1);
                const vBL = (j + 1) * width + i;
                const vBR = (j + 1) * width + (i + 1);
                if (!isNoData(j, i) && !isNoData(j, i + 1) &&
                    !isNoData(j + 1, i) && !isNoData(j + 1, i + 1)) {
                    indices.push(vTL, vBL, vTR);
                    indices.push(vTR, vBL, vBR);
                }
            }
        }
    }

    geometry.attributes.position.needsUpdate = true;

    geometry.setIndex(indices);
    geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
    geometry.computeVertexNormals();

    if (geometry.attributes.normal) {
        geometry.attributes.normal.needsUpdate = true;
    }

    // Height texture for per-pixel shading, shadows and ambient occlusion:
    // the high-resolution grid when the engine sent one, else the mesh grid.
    const tex = _buildHeightTexture(data);
    const heightTexture = tex.texture;

    // ShaderMaterial customized with standard parameters and heightmap texture
    const material = createCustomMaterial(true, null, heightTexture, tex.width, tex.height);
    material.uniforms.uModelSize.value.set(modelWidth * scaleFactor, modelHeight * scaleFactor);
    material.uniforms.uRealHeightRange.value = elevRange * scaleFactor;
    material.uniforms.uHeightRange.value = elevRange * baseExaggeration * currentZScale * scaleFactor;
    _setupLightingBake(heightTexture, tex.width, tex.height);

    const terrainMesh = new THREE.Mesh(geometry, material);
    scene.add(terrainMesh);
    registerObject(data.element_id, terrainMesh, "raster");

    // ── Block base ───────────────────────────────────────────────────────
    const baseZ = -(maxDim * scaleFactor) * baseThicknessFraction;
    _buildSole(geometry, baseZ);
    _buildWalls(data, baseZ, maxDim);

    // ── Camera positioning ───────────────────────────────────────────────
    camera.near = 0.1;
    camera.far = 1000.0;
    camera.updateProjectionMatrix();

    const midZ = (((data.z_max - data.z_min) * baseExaggeration * currentZScale) / 2) * scaleFactor;
    controls.target.set(0, 0, midZ);
    camera.position.set(0, -120, 100);
    camera.lookAt(0, 0, midZ);
    controls.update();

    // Reposition scene lights above the model at standard scale 100
    scene.traverse(obj => {
        if (obj.isDirectionalLight && obj.name !== "ambient_default") {
            const p = _azimuthAltitudeToXYZ(135, 45, 150);
            obj.position.set(p.x, p.y, p.z);
            scene.add(obj.target); 
        }
        if (obj.isHemisphereLight) {
            obj.position.set(0, 0, 120);
        }
    });

    // --- Preserve Symbology Render Mode Across Layer Changes --- [Bug D Fix]
    if (colorMode === "classified") {
        _applyClassifiedColors();
    } else if (colorMode === "solid") {
        const fallbackSolidColor = "#4a90d9";
        _applySolidColor(fallbackSolidColor);
    } else if (colorMode === "natural") {
        _applyColormap();          // keeps vertex colours ready for a switch back
        _applyNaturalColors();
    } else {
        _applyColormap();
    }
    _applySkyGradient();           // haze colour of the new materials

    _updateLegendWidget();

    _updateStatus(`Scene: ${data.label || data.element_id} loaded (${width}×${height} vertices).`);
}

// ---------------------------------------------------------------------------
// BLOCK BASE — sole (bottom plate)
// ---------------------------------------------------------------------------

function _buildSole(terrainGeometry, baseZ) {
    unregisterObject("block_base_sole");

    const geo = terrainGeometry.clone();
    const pos = geo.attributes.position.array;
    for (let i = 2; i < pos.length; i += 3) pos[i] = baseZ;
    geo.attributes.position.needsUpdate = true;
    geo.computeVertexNormals();

    // Use structural material instead of terrain material
    const mat = createStructuralMaterial(baseColor);

    const mesh = new THREE.Mesh(geo, mat);
    mesh.visible = blockBaseVisible; // Apply the cached visibility state
    scene.add(mesh);
    registerObject("block_base_sole", mesh, "sole");
}

// ---------------------------------------------------------------------------
// BLOCK BASE — lateral walls (boundary edge detection)
// ---------------------------------------------------------------------------

function _buildWalls(data, baseZ, maxDim) {
    unregisterObject("block_base_walls");
    wallVertexMappings = [];

    const width = data.width;
    const height = data.height;
    const mW = Math.abs(data.x_coords[data.x_coords.length - 1] - data.x_coords[0]);
    const mH = Math.abs(data.y_coords[data.y_coords.length - 1] - data.y_coords[0]);

    const isNoData = (r, c) => {
        const v = data.z_values[r][c];
        return v === null || v === undefined || isNaN(v) || v === data.nodata_value;
    };

    // Build a boolean validity grid for cells (all 4 corners must be valid)
    const validCells = [];
    for (let j = 0; j < height - 1; j++) {
        validCells[j] = new Uint8Array(width - 1);
        for (let i = 0; i < width - 1; i++) {
            validCells[j][i] = (
                !isNoData(j, i) && !isNoData(j, i + 1) &&
                !isNoData(j + 1, i) && !isNoData(j + 1, i + 1)
            ) ? 1 : 0;
        }
    }

    const wallPositions = [];

    const addWallQuad = (rA, cA, rB, cB) => {
        const rawZA = data.z_values[rA][cA];
        const rawZB = data.z_values[rB][cB];

        const xa = (cA / (width - 1) - 0.5) * mW * scaleFactor;
        const ya = (0.5 - rA / (height - 1)) * mH * scaleFactor;
        const za = (rawZA - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;

        const xb = (cB / (width - 1) - 0.5) * mW * scaleFactor;
        const yb = (0.5 - rB / (height - 1)) * mH * scaleFactor;
        const zb = (rawZB - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;

        // Capture logical vertex indices BEFORE pushing new vertices
        const idxTopA = wallPositions.length / 3;       // vertex 0 of this quad
        const idxTopB1 = idxTopA + 2;                   // vertex 2 (TopB, tri 1)
        const idxTopB2 = idxTopA + 3;                   // vertex 3 (TopB, tri 2)

        // Triangle 1: TopA, BottomA, TopB
        wallPositions.push(xa, ya, za);   // idxTopA   ← top vertex, needs Z-scale sync
        wallPositions.push(xa, ya, baseZ);
        wallPositions.push(xb, yb, zb);   // idxTopB1  ← top vertex, needs Z-scale sync

        // Triangle 2: TopB, BottomA, BottomB
        wallPositions.push(xb, yb, zb);   // idxTopB2  ← top vertex, needs Z-scale sync
        wallPositions.push(xa, ya, baseZ);
        wallPositions.push(xb, yb, baseZ);

        // Register the top vertices for synchronized Z-scale updates
        wallVertexMappings.push({ rawZ: rawZA, vertexIndex: idxTopA });
        wallVertexMappings.push({ rawZ: rawZB, vertexIndex: idxTopB1 });
        wallVertexMappings.push({ rawZ: rawZB, vertexIndex: idxTopB2 });
    };

    // Scan horizontal edges
    for (let j = 0; j < height; j++) {
        for (let i = 0; i < width - 1; i++) {
            if (isNoData(j, i) || isNoData(j, i + 1)) continue;
            const below = (j < height - 1) ? validCells[j][i] : 0;
            const above = (j > 0) ? validCells[j - 1][i] : 0;
            if (below !== above) {
                if (below) addWallQuad(j, i, j, i + 1);
                else addWallQuad(j, i + 1, j, i);
            }
        }
    }

    // Scan vertical edges
    for (let j = 0; j < height - 1; j++) {
        for (let i = 0; i < width; i++) {
            if (isNoData(j, i) || isNoData(j + 1, i)) continue;
            const right = (i < width - 1) ? validCells[j][i] : 0;
            const left = (i > 0) ? validCells[j][i - 1] : 0;
            if (right !== left) {
                if (right) addWallQuad(j + 1, i, j, i);
                else addWallQuad(j, i, j + 1, i);
            }
        }
    }

    if (wallPositions.length === 0) return;

    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(wallPositions, 3));
    geo.computeVertexNormals();

    // Use structural material with wall-specific color
    const mat = createStructuralMaterial(wallsColor);
    wallMesh = new THREE.Mesh(geo, mat);
    wallMesh.visible = blockBaseVisible; // Apply the cached visibility state
    scene.add(wallMesh);
    registerObject("block_base_walls", wallMesh, "walls");
}

// Rebuild block base after thickness change (requires valid data cache)
function _rebuildBlockBase() {
    const terrainObj = _findTerrainObject();
    if (!terrainObj) return;

    const data = _buildDataFromCache();
    const maxDim = _computeMaxDim(data);
    const baseZ = -maxDim * scaleFactor * baseThicknessFraction;

    _buildSole(terrainObj.mesh.geometry, baseZ);
    _buildWalls(data, baseZ, maxDim);
}

// ---------------------------------------------------------------------------
// Z-SCALE UPDATE
// ---------------------------------------------------------------------------

function _updateZScale(scale) {
    currentZScale = scale;
    const terrainObj = _findTerrainObject();
    if (!terrainObj || !originalDEMValues) return;

    const geo = terrainObj.mesh.geometry;
    const positions = geo.attributes.position.array;
    const height = originalDEMValues.length;
    const width = originalDEMValues[0].length;
    let vi = 0;
    for (let j = 0; j < height; j++) {
        for (let i = 0; i < width; i++) {
            const rawZ = originalDEMValues[j][i];
            if (rawZ !== null && !isNaN(rawZ)) {
                positions[vi + 2] = (rawZ - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;
            }
            vi += 3;
        }
    }
    geo.attributes.position.needsUpdate = true;
    geo.computeVertexNormals();
    if (geo.attributes.normal) {
        geo.attributes.normal.needsUpdate = true;
    }

    // Update ONLY the top vertices of the wall mesh using corrected index mapping
    if (wallMesh) {
        const wallGeo = wallMesh.geometry;
        const wallPos = wallGeo.attributes.position.array;
        for (const m of wallVertexMappings) {
            // zOffsetOf guarantees we write to Z, not X
            wallPos[zOffsetOf(m.vertexIndex)] =
                (m.rawZ - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;
        }
        wallGeo.attributes.position.needsUpdate = true;
        wallGeo.computeVertexNormals();
        if (wallGeo.attributes.normal) {
            wallGeo.attributes.normal.needsUpdate = true;
        }
    }

    // Sync the Z-Scale uniform value with the GPU fragment shader
    if (terrainObj.mesh.material && terrainObj.mesh.material.uniforms && terrainObj.mesh.material.uniforms.uZScale) {
        const uniforms = terrainObj.mesh.material.uniforms;
        uniforms.uZScale.value = scale;
        uniforms.uHeightRange.value = Math.max(1.0, elevationStats.max - elevationStats.min)
            * baseExaggeration * currentZScale * scaleFactor;
        bakeDirty = true;   // shadows and sky-view factor follow the exaggeration
    }


    // --- Dynamic Vector Scale Updates ---
    for (const id in sceneObjects) {
        const obj = sceneObjects[id];
        if (!obj || !obj.mesh) continue;

        if (obj.type === "vector" && obj.descriptor) {
            _updateVectorGeometryZ(obj.mesh, obj.descriptor);
        } else if (obj.type === "curtain" && obj.descriptor) {
            _updateCurtainGeometryZ(obj.mesh, obj.descriptor);
        } else if (obj.type === "point_marker" && obj.descriptor) {
            _updatePointMarkerZ(obj.mesh, obj.descriptor);
        } else if (obj.type === "filled_polygon" && obj.descriptor) {
            _updateFilledPolygonZ(obj.mesh, obj.descriptor);
        }
    }
}

function _updateVectorGeometryZ(mesh, descriptor) {
    const posAttr = mesh.geometry.attributes.position;
    const positions = posAttr.array;

    const zOffset = descriptor.height_offset || 0.0;
    let idx = 0;
    descriptor.vertices.forEach(([x, y, z]) => {
        positions[idx + 2] = ((z + zOffset) - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;
        idx += 3;
    });
    posAttr.needsUpdate = true;
    mesh.geometry.computeBoundingSphere();
}

function _updateCurtainGeometryZ(mesh, descriptor) {
    const posAttr = mesh.geometry.attributes.position;
    const positions = posAttr.array;

    // Scale real-world depth meters to match exaggerated terrain vertical axis [Fix 3]
    const scaledDepth = (descriptor.extrude_depth || 0.0) * baseExaggeration * currentZScale * scaleFactor;
    const zOffset = descriptor.height_offset || 0.0;

    let idx = 0;
    descriptor.vertices.forEach(([x, y, z]) => {
        const zTop = ((z + zOffset) - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;
        const zBottom = zTop - scaledDepth;
        positions[idx + 2] = zTop;
        positions[idx + 5] = zBottom;
        idx += 6;
    });
    posAttr.needsUpdate = true;
    mesh.geometry.computeVertexNormals();
    if (mesh.geometry.attributes.normal) {
        mesh.geometry.attributes.normal.needsUpdate = true;
    }
    mesh.geometry.computeBoundingSphere();
}

function _updatePointMarkerZ(mesh, descriptor) {
    const [x, y, z] = descriptor.vertices[0];
    const zOffset = descriptor.height_offset || 0.0;
    const zDraped = ((z + zOffset) - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;
    const floatOffset = modelMaxDim * 0.005;
    mesh.position.z = zDraped + floatOffset;
}

function _updateFilledPolygonZ(mesh, descriptor) {
    const posAttr = mesh.geometry.attributes.position;
    const positions = posAttr.array;
    const zOffset = descriptor.height_offset || 0.0;

    const scaledBoundaries = [];
    descriptor.vertices.forEach(([x, y, z]) => {
        const zScaled = ((z + zOffset) - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;
        scaledBoundaries.push({
            x: (x - spatialOffsets.x) * scaleFactor,
            y: (y - spatialOffsets.y) * scaleFactor,
            z: zScaled
        });
    });

    for (let i = 0; i < posAttr.count; i++) {
        const x = positions[i * 3];
        const y = positions[i * 3 + 1];

        let closestZ = scaledBoundaries[0].z;
        let minDist = Infinity;
        scaledBoundaries.forEach(v => {
            const d = Math.hypot(v.x - x, v.y - y);
            if (d < minDist) {
                minDist = d;
                closestZ = v.z;
            }
        });
        positions[i * 3 + 2] = closestZ + modelMaxDim * 0.002;
    }
    posAttr.needsUpdate = true;
    mesh.geometry.computeVertexNormals();
    mesh.geometry.computeBoundingSphere();
}


// ---------------------------------------------------------------------------
// COLORMAP SYSTEM
// ---------------------------------------------------------------------------

/**
 * Scientific colormaps as normalized RGB stop tables.
 * Values are linear RGB [0..1], matching Matplotlib's sRGB output.
 * Add more entries here without touching any other code.
 */
const COLORMAPS = typeof COLORMAPS_ALL !== "undefined" ? COLORMAPS_ALL : {};

/**
 * Sample a colormap ramp at position t ∈ [0, 1].
 * Returns { r, g, b } in [0, 1].
* Fallback to a grayscale ramp if colormap definitions are missing.
 */
function _sampleRamp(stops, t) {
    t = Math.max(0, Math.min(1, t));

    // Safeguard against missing, null, or empty colormap arrays
    if (!stops || !Array.isArray(stops) || stops.length === 0) {
        return { r: t, g: t, b: t }; // Neutral grayscale fallback
    }

    if (stops.length === 1) {
        return { r: stops[0].r, g: stops[0].g, b: stops[0].b };
    }

    for (let i = 0; i < stops.length - 1; i++) {
        if (t >= stops[i].pos && t <= stops[i + 1].pos) {
            const range = stops[i + 1].pos - stops[i].pos;
            const local = (range > 0) ? (t - stops[i].pos) / range : 0;
            return {
                r: stops[i].r + local * (stops[i + 1].r - stops[i].r),
                g: stops[i].g + local * (stops[i + 1].g - stops[i].g),
                b: stops[i].b + local * (stops[i + 1].b - stops[i].b),
            };
        }
    }
    const last = stops[stops.length - 1];
    return { r: last.r, g: last.g, b: last.b };
}

/** Normalize a raw elevation value to [0, 1] using current bounds. */
function _normZ(rawZ, data) {
    const lo = colorBoundsAuto ? (data ? data.z_min : elevationStats.min) : colorBoundsMin;
    const hi = colorBoundsAuto ? (data ? data.z_max : elevationStats.max) : colorBoundsMax;
    const range = Math.max(1e-6, hi - lo);
    const t = (rawZ - lo) / range;
    return isNaN(t) || !isFinite(t) ? 0 : t;
}

/** Recompute and apply the current colormap to the terrain mesh. */
function _applyColormap() {
    const terrainObj = _findTerrainObject();
    if (!terrainObj || !originalDEMValues) return;

    // Dynamically fallback to the single embedded colormap active_ramp if running offline
    const ramp = (typeof ACTIVE_SCENE_DATA !== "undefined" && ACTIVE_SCENE_DATA.style && ACTIVE_SCENE_DATA.style.active_ramp)
        ? ACTIVE_SCENE_DATA.style.active_ramp
        : (COLORMAPS[currentColormapName] || COLORMAPS.viridis);
    const colors = [];

    for (let j = 0; j < originalDEMValues.length; j++) {
        for (let i = 0; i < originalDEMValues[j].length; i++) {
            const rawZ = originalDEMValues[j][i];
            let t = _normZ(rawZ ?? elevationStats.min, null);
            if (currentColormapReverse) t = 1 - t;
            const c = _sampleRamp(ramp, t);
            colors.push(c.r, c.g, c.b);
        }
    }

    const geo = terrainObj.mesh.geometry;
    geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
    geo.attributes.color.needsUpdate = true;

    const uniforms = terrainObj.mesh.material.uniforms;
    if (uniforms) {
        uniforms.uUseVertexColors.value = true;
        uniforms.uLandCover.value = false;
    }

    _updateLegendWidget();
}

/**
 * Natural land-cover colouring: computed in the shader from elevation,
 * slope and aspect. ``settings`` (all optional) = {tree_line_m,
 * snow_line_m (null = automatic), rock_slope_deg, colors: {forest, grass,
 * rock, snow}}; missing entries keep their current value.
 */
function _applyNaturalColors(settings) {
    const p = settings || {};
    if ("tree_line_m" in p) realism.treeLineM = p.tree_line_m;
    if ("snow_line_m" in p) realism.snowLineM = p.snow_line_m;
    if (p.rock_slope_deg !== undefined && p.rock_slope_deg !== null) realism.rockSlopeDeg = p.rock_slope_deg;
    if (p.colors) Object.assign(realism.landColors, p.colors);
    _applyRealismUniforms();
    _updateLegendWidget();
}

/** Apply a flat solid color to the terrain (disables vertex colors). */
function _applySolidColor(hexColor) {
    const terrainObj = _findTerrainObject();
    if (!terrainObj) return;
    const uniforms = terrainObj.mesh.material.uniforms;
    if (uniforms) {
        uniforms.uUseVertexColors.value = false;
        uniforms.uLandCover.value = false;
        uniforms.uSolidColor.value.set(hexColor);
    }
    _updateLegendWidget();
}


/** Recompute class intervals and map discrete colors to each vertex buffer location. */
function _applyClassifiedColors() {
    const terrainObj = _findTerrainObject();
    if (!terrainObj || !originalDEMValues || currentClassColors.length === 0) return;

    const colors = [];
    const numClasses = currentClassColors.length;

    for (let j = 0; j < originalDEMValues.length; j++) {
        for (let i = 0; i < originalDEMValues[j].length; i++) {
            const rawZ = originalDEMValues[j][i];
            let zVal = rawZ ?? elevationStats.min;

            // Discretize: find matching class bucket
            let classIdx = 0;
            for (let c = 0; c < numClasses - 1; c++) {
                if (zVal > currentClassBounds[c]) {
                    classIdx = c + 1;
                } else {
                    break;
                }
            }

            const cColor = new THREE.Color(currentClassColors[classIdx]);
            colors.push(cColor.r, cColor.g, cColor.b);
        }
    }

    const geo = terrainObj.mesh.geometry;
    geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
    geo.attributes.color.needsUpdate = true;

    const uniforms = terrainObj.mesh.material.uniforms;
    if (uniforms) {
        uniforms.uUseVertexColors.value = true;
        uniforms.uLandCover.value = false;
    }

    _updateLegendWidget();
}


// ---------------------------------------------------------------------------
// BLOCK COLOR SETTERS
// ---------------------------------------------------------------------------

function _setObjectColor(elementId, hexColor) {
    const obj = sceneObjects[elementId];
    if (obj && obj.mesh.material && obj.mesh.material.uniforms) {
        obj.mesh.material.uniforms.uSolidColor.value.set(hexColor);
    }
}

// ---------------------------------------------------------------------------
// SKY GRADIENT
// ---------------------------------------------------------------------------

function _applySkyGradient() {
    const container = document.getElementById("viewport-container");
    if (container) {
        const topHex = "#" + skyColorTop.getHexString();
        const bottomHex = "#" + skyColorBottom.getHexString();
        container.style.background =
            `linear-gradient(to bottom, ${topHex} 0%, ${bottomHex} 100%)`;
    }
    if (renderer) renderer.setClearColor(0x000000, 0);
    if (scene) scene.background = null;

    // Distant relief fades towards the horizon colour of the sky (lightened,
    // as real haze is brighter than the sky above it).
    const haze = skyColorBottom.clone().lerp(new THREE.Color(0.80, 0.85, 0.92), 0.55);
    for (const id in sceneObjects) {
        const u = sceneObjects[id].mesh && sceneObjects[id].mesh.material && sceneObjects[id].mesh.material.uniforms;
        if (u && u.uHazeColor) u.uHazeColor.value.copy(haze);
    }
}

// ---------------------------------------------------------------------------
// LIGHT MANAGER
// ---------------------------------------------------------------------------

function _azimuthAltitudeToXYZ(azimuthDeg, altitudeDeg, radius) {
    const az = azimuthDeg * Math.PI / 180;   
    const alt = altitudeDeg * Math.PI / 180;
    return {
        x: radius * Math.cos(alt) * Math.sin(az),
        y: radius * Math.cos(alt) * Math.cos(az),
        z: radius * Math.sin(alt),
    };
}

const TYPE_MAP = {
    directional: THREE.DirectionalLight,
    point: THREE.PointLight,
    spot: THREE.SpotLight,
};

function _addLight(descriptor) {
    const LightClass = TYPE_MAP[descriptor.type] || THREE.DirectionalLight;
    const light = new LightClass(
        new THREE.Color(descriptor.color).getHex(),
        descriptor.intensity * 5.0
    );
    light.name = descriptor.id;

    const radius = modelMaxDim * 1.5;
    const p = _azimuthAltitudeToXYZ(descriptor.azimuth, descriptor.altitude, radius);
    light.position.set(p.x, p.y, p.z);

    if (descriptor.type === "spot") {
        light.angle = 30 * Math.PI / 180; 
        light.penumbra = 0.2;
    }

    scene.add(light);
    scene.add(light.target);
    sceneObjects[descriptor.id] = { mesh: light, type: "light", visible: true };

    if (descriptor.gizmo) _showLightGizmo(descriptor.id, light);
}

function _updateLight(descriptor) {
    const obj = sceneObjects[descriptor.id];
    if (!obj) {
        _addLight(descriptor);
        return;
    }

    const light = obj.mesh;
    light.color.set(descriptor.color);
    light.intensity = descriptor.intensity * 5.0;

    const radius = modelMaxDim * 1.5;
    const p = _azimuthAltitudeToXYZ(descriptor.azimuth, descriptor.altitude, radius);
    light.position.set(p.x, p.y, p.z);

    // Sync authoritative custom shader parameters if modifying the primary light source
    if (descriptor.id === "light_0") {
        sunColor.set(descriptor.color);
        sunIntensity = descriptor.intensity;
        sunDirection.copy(light.position).normalize();
    }

    // Force shader updates across all active materials
    updateSceneUniforms();

    // Manage gizmo visibility
    const gizmoId = descriptor.id + "_gizmo";
    if (descriptor.gizmo) {
        _showLightGizmo(descriptor.id, light);
    } else {
        unregisterObject(gizmoId);
    }
}

/** Show a small visual helper sphere at the light position. */
function _showLightGizmo(lightId, light) {
    const gizmoId = lightId + "_gizmo";
    unregisterObject(gizmoId);

    const geo = new THREE.SphereGeometry(modelMaxDim * 0.02, 8, 8);
    const mat = new THREE.MeshBasicMaterial({ color: light.color });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.copy(light.position);
    scene.add(mesh);
    sceneObjects[gizmoId] = { mesh, type: "gizmo", visible: true };
}

// ---------------------------------------------------------------------------
// VECTOR FEATURES
// ---------------------------------------------------------------------------

function _buildVectorFeature(descriptor) {
    if (!descriptor.vertices || descriptor.vertices.length < 1) return;

    // ── 1. INDEPENDENT COLOR CALCULATION ──
    let featureColor = descriptor.color || "#3498db";

    // Check if color is driven by an attribute
    if (descriptor.color_styling === "attribute" && descriptor.attribute_values && descriptor.attribute_bounds) {
        // Extract the color value (index 0)
        const val = descriptor.attribute_values[0];
        const minVal = descriptor.attribute_bounds.min;
        const maxVal = descriptor.attribute_bounds.max;
        const norm = (val - minVal) / (maxVal - minVal || 1.0);

        const rampName = descriptor.vector_colormap || "terrain";
        const ramp = COLORMAPS[rampName] || COLORMAPS.terrain || COLORMAPS.viridis;
        const sampled = _sampleRamp(ramp, Math.max(0, Math.min(1, norm)));

        featureColor = "#" +
            Math.round(sampled.r * 255).toString(16).padStart(2, "0") +
            Math.round(sampled.g * 255).toString(16).padStart(2, "0") +
            Math.round(sampled.b * 255).toString(16).padStart(2, "0");
    }
    descriptor.resolved_color = featureColor;

    // ── 2. INDEPENDENT WIDTH/SIZE CALCULATION ──
    let scaleMultiplier = 1.0;

    // Check if dimension size is driven by an attribute
    if (descriptor.width_styling === "attribute" && descriptor.attribute_values && descriptor.attribute_bounds) {
        // Extract the size value (index 1)
        const sizeVal = descriptor.attribute_values[1];
        const minSizeVal = descriptor.attribute_bounds.min_size || 0.0;
        const maxSizeVal = descriptor.attribute_bounds.max_size || 1.0;
        const normSize = (sizeVal - minSizeVal) / (maxSizeVal - minSizeVal || 1.0);

        // Scale proportionally within limits up to max_size_scale (e.g., up to 5.0x)
        const maxLimit = descriptor.max_size_scale || 5.0;
        scaleMultiplier = 1.0 + Math.max(0, Math.min(1, normSize)) * (maxLimit - 1.0);
    } else {
        // Fall back to the constant base size slider (scaled to WebGL coordinates)
        scaleMultiplier = descriptor.base_fixed_size || 1.5;
    }

    // ── 3. GEOMETRY GENERATION WITH ASSIGNED ATTRIBUTES ──
    if (descriptor.geom_type === "point") {
        _buildPointMarker(descriptor, scaleMultiplier);
        return;
    }
    if (descriptor.geom_type === "filled") {
        _buildFilledPolygon(descriptor, scaleMultiplier); 
        return;
    }

    if (descriptor.vertices.length < 2) return;

    const zOffset = descriptor.height_offset || 0.0;
    const positions = [];
    descriptor.vertices.forEach(([x, y, z]) => {
        positions.push(
            (x - spatialOffsets.x) * scaleFactor,
            (y - spatialOffsets.y) * scaleFactor,
            ((z + zOffset) - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor
        );
    });

    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));

    const mat = new THREE.LineBasicMaterial({
        color: descriptor.resolved_color,
        // Line-width is scaled independently of color variables
        linewidth: Math.max(1, scaleMultiplier)
    });
    const line = new THREE.Line(geo, mat);
    scene.add(line);

    sceneObjects[descriptor.element_id] = {
        mesh: line,
        type: "vector",
        visible: true,
        descriptor: descriptor
    };

    if (descriptor.geom_type === "curtain") {
        _buildExtrudedCurtain(descriptor, positions, scaleMultiplier);
    }
}

function _buildExtrudedCurtain(descriptor, topPositions, factor) {
    const curtainPositions = [];
    const baseDepth = descriptor.extrude_depth || 0.0;

    // Scale curtain vertical depth dynamically based on the attribute factor (e.g. fault displacement)
    const activeDepth = (descriptor.width_styling === "attribute")
        ? factor * baseDepth
        : baseDepth;

    const scaledDepth = activeDepth * baseExaggeration * currentZScale * scaleFactor;

    for (let i = 0; i < topPositions.length; i += 3) {
        const x = topPositions[i];
        const y = topPositions[i + 1];
        const zTop = topPositions[i + 2];
        curtainPositions.push(x, y, zTop);
        curtainPositions.push(x, y, zTop - scaledDepth);
    }

    const indices = [];
    const n = topPositions.length / 3;
    for (let i = 0; i < n - 1; i++) {
        const tL = i * 2, tR = (i + 1) * 2;
        const bL = i * 2 + 1, bR = (i + 1) * 2 + 1;
        indices.push(tL, bL, tR, tR, bL, bR);
    }

    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(curtainPositions, 3));
    geo.setIndex(indices);
    geo.computeVertexNormals();

    const mat = createStructuralMaterial(new THREE.Color(descriptor.resolved_color));
    const mesh = new THREE.Mesh(geo, mat);
    scene.add(mesh);

    sceneObjects[descriptor.element_id + "_curtain"] = {
        mesh: mesh,
        type: "curtain",
        visible: true,
        descriptor: descriptor
    };
}

function _buildPointMarker(descriptor, factor) {
    const [x, y, z] = descriptor.vertices[0];
    const zDraped = (z - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;
    const floatOffset = modelMaxDim * 0.005;

    // Scale point marker size dynamically based on the attribute factor
    const baseSize = descriptor.point_marker_size || 1.0;
    const sizeMultiplier = (descriptor.width_styling === "attribute")
        ? 0.3 + factor * (baseSize - 0.3)
        : baseSize;

    const geo = new THREE.SphereGeometry(modelMaxDim * 0.015 * sizeMultiplier, 12, 12);
    const mat = createStructuralMaterial(new THREE.Color(descriptor.resolved_color));
    const mesh = new THREE.Mesh(geo, mat);

    mesh.position.set(
        (x - spatialOffsets.x) * scaleFactor,
        (y - spatialOffsets.y) * scaleFactor,
        zDraped + floatOffset
    );

    scene.add(mesh);
    sceneObjects[descriptor.element_id] = {
        mesh: mesh,
        type: "point_marker",
        visible: true,
        descriptor: descriptor
    };
}

function _buildFilledPolygon(descriptor, factor) {
    if (!descriptor.vertices || descriptor.vertices.length < 3) return;
    // 1. Construct 2D flat shape
    const shape = new THREE.Shape();
    const positions3D = [];

    descriptor.vertices.forEach(([x, y, z], index) => {
        const xLocal = (x - spatialOffsets.x) * scaleFactor;
        const yLocal = (y - spatialOffsets.y) * scaleFactor;
        const zLocal = (z - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;

        if (index === 0) {
            shape.moveTo(xLocal, yLocal);
        } else {
            shape.lineTo(xLocal, yLocal);
        }
        positions3D.push(new THREE.Vector3(xLocal, yLocal, zLocal));
    });
    shape.closePath();

    const geo = new THREE.ShapeGeometry(shape);
    const posAttr = geo.attributes.position;
    const positions = posAttr.array;
    // 2. Project vertices onto boundary elevations with a slight height offset to prevent z-fighting
    for (let i = 0; i < posAttr.count; i++) {
        const x = positions[i * 3];
        const y = positions[i * 3 + 1];

        let closestZ = positions3D[0].z;
        let minDist = Infinity;
        positions3D.forEach(v => {
            const d = Math.hypot(v.x - x, v.y - y);
            if (d < minDist) {
                minDist = d;
                closestZ = v.z;
            }
        });
        positions[i * 3 + 2] = closestZ + modelMaxDim * 0.002;
    }

    geo.computeVertexNormals();

    // Scale polygon opacity dynamically based on the attribute factor
    const baseOpacity = descriptor.polygon_opacity || 0.5;
    const activeOpacity = (descriptor.width_styling === "attribute")
        ? 0.15 + factor * (baseOpacity - 0.15)
        : baseOpacity;

    const color = new THREE.Color(descriptor.resolved_color);
    const mat = new THREE.MeshBasicMaterial({
        color: color,
        transparent: true,
        opacity: activeOpacity,
        side: THREE.DoubleSide,
        depthWrite: false
    });

    const mesh = new THREE.Mesh(geo, mat);
    scene.add(mesh);

    sceneObjects[descriptor.element_id] = {
        mesh: mesh,
        type: "filled_polygon",
        visible: true,
        descriptor: descriptor,
        positions3D: positions3D
    };
}


function _applyDynamicVectorStyle(style) {
    // Append an underscore to resolve prefix collision boundaries (e.g., vector_1 vs vector_11)
    const targetPrefix = style.element_id + "_";

    // 1. Check if the visualization mode (geom_type) changed.
    let needsRebuild = false;
    for (const id in sceneObjects) {
        if (id.startsWith(targetPrefix)) {
            const obj = sceneObjects[id];
            if (obj && obj.descriptor && obj.descriptor.geom_type !== style.geom_type) {
                needsRebuild = true;
                break;
            }
        }
    }

    if (needsRebuild) {
        const descriptorsToRebuild = [];
        const seenIds = new Set();
        for (const id in sceneObjects) {
            if (id.startsWith(targetPrefix)) {
                const obj = sceneObjects[id];
                if (obj && obj.descriptor && !seenIds.has(obj.descriptor.element_id)) {
                    seenIds.add(obj.descriptor.element_id);
                    descriptorsToRebuild.push(obj.descriptor);
                }
            }
        }

        const keysToRemove = [];
        for (const id in sceneObjects) {
            if (id.startsWith(targetPrefix)) {
                keysToRemove.push(id);
            }
        }
        keysToRemove.forEach(key => unregisterObject(key));

        descriptorsToRebuild.forEach(desc => {
            desc.geom_type = style.geom_type;
            desc.extrude_depth = style.extrude_depth;
            desc.polygon_opacity = style.polygon_opacity;
            desc.point_marker_size = style.point_marker_size;
            desc.height_offset = style.height_offset;
            desc.color_styling = style.color_styling;
            desc.vector_colormap = style.vector_colormap;
            desc.color = style.fixed_color;
            desc.width_styling = style.width_styling;
            desc.base_fixed_size = style.base_fixed_size;
            desc.max_size_scale = style.max_size_scale;
            desc.size_attribute = style.size_attribute;

            _buildVectorFeature(desc);
        });
        return;
    }

    // 2. If mode is unchanged, perform safe real-time property mutations:
    for (const id in sceneObjects) {
        if (!id.startsWith(targetPrefix)) continue;

        const obj = sceneObjects[id];
        if (!obj || !obj.mesh || !obj.descriptor) continue;

        const mesh = obj.mesh;
        const desc = obj.descriptor;

        desc.geom_type = style.geom_type;
        desc.extrude_depth = style.extrude_depth;
        desc.polygon_opacity = style.polygon_opacity;
        desc.point_marker_size = style.point_marker_size;
        desc.height_offset = style.height_offset;
        desc.color_styling = style.color_styling;
        desc.width_styling = style.width_styling; // Sync the new sizing mode
        desc.vector_colormap = style.vector_colormap;

        // Recalculate dynamic color (same as before)
        let resolvedColor = style.fixed_color || "#3498db";
        if (style.color_styling === "attribute" && desc.attribute_values && desc.attribute_bounds) {
            const val = desc.attribute_values[0];
            const minVal = desc.attribute_bounds.min;
            const maxVal = desc.attribute_bounds.max;
            const norm = (val - minVal) / (maxVal - minVal || 1.0);

            const ramp = COLORMAPS[style.vector_colormap] || COLORMAPS.terrain || COLORMAPS.viridis;
            const sampled = _sampleRamp(ramp, norm);

            resolvedColor = "#" +
                Math.round(sampled.r * 255).toString(16).padStart(2, "0") +
                Math.round(sampled.g * 255).toString(16).padStart(2, "0") +
                Math.round(sampled.b * 255).toString(16).padStart(2, "0");
        }

        desc.color = resolvedColor;

        // Recalculate dynamic sizing factor
        let factor = 1.0;
        if (style.width_styling === "attribute" && desc.attribute_values && desc.attribute_bounds) {
            const val = desc.attribute_values[0];
            const minVal = desc.attribute_bounds.min;
            const maxVal = desc.attribute_bounds.max;
            factor = (val - minVal) / (maxVal - minVal || 1.0);
            factor = Math.max(0.0, Math.min(1.0, factor));
        }

        if (obj.type === "vector") {
            if (mesh.material.color) {
                mesh.material.color.set(resolvedColor);
            }
            // Recalculate dynamic linewidth in real-time
            const activeLineWidth = (style.width_styling === "attribute")
                ? 1.0 + factor * (style.line_width - 1.0)
                : style.line_width;

            mesh.material.linewidth = activeLineWidth;
            mesh.material.needsUpdate = true;
            _updateVectorGeometryZ(mesh, desc);
        }
        else if (obj.type === "curtain") {
            if (mesh.material.uniforms && mesh.material.uniforms.uSolidColor) {
                mesh.material.uniforms.uSolidColor.value.set(resolvedColor);
            } else if (mesh.material.color) {
                mesh.material.color.set(resolvedColor);
            }
            mesh.material.needsUpdate = true;
            _updateCurtainGeometryZ(mesh, desc); // uZScale inside this helper will automatically scale curtain depth
        }
        else if (obj.type === "filled_polygon") {
            if (mesh.material.color) {
                mesh.material.color.set(resolvedColor);
            }
            // Recalculate dynamic opacity in real-time
            const activeOpacity = (style.width_styling === "attribute")
                ? 0.15 + factor * (style.polygon_opacity - 0.15)
                : style.polygon_opacity;

            mesh.material.opacity = activeOpacity;
            mesh.material.needsUpdate = true;
            _updateFilledPolygonZ(mesh, desc);
        }
        else if (obj.type === "point_marker") {
            if (mesh.material.uniforms && mesh.material.uniforms.uSolidColor) {
                mesh.material.uniforms.uSolidColor.value.set(resolvedColor);
            } else if (mesh.material.color) {
                mesh.material.color.set(resolvedColor);
            }
            mesh.material.needsUpdate = true;

            // Recalculate dynamic point size scale in real-time
            const sizeMultiplier = (style.width_styling === "attribute")
                ? 0.3 + factor * (style.point_marker_size - 0.3)
                : style.point_marker_size;

            mesh.scale.setScalar(sizeMultiplier);
            _updatePointMarkerZ(mesh, desc);
        }
    }
}

// ---------------------------------------------------------------------------
// SHADING MODE (legacy compatibility)
// ---------------------------------------------------------------------------

/**
 * Switch shading models dynamically inside the custom GLSL ShaderMaterial.
 */
function _updateShadingMode(modeIndex) {
    currentShadingMode = modeIndex;
    const terrainObj = _findTerrainObject();
    if (!terrainObj || !terrainObj.mesh.material) return;

    // Direct uniform binding to the GPU thread
    if (terrainObj.mesh.material.uniforms && terrainObj.mesh.material.uniforms.uZScale) {
        terrainObj.mesh.material.uniforms.uHeightmap.value = terrainObj.mesh.material.uniforms.uHeightmap.value; // Keep texture alive
        terrainObj.mesh.material.uniforms.uMultidirectional.value = globalMultidirectional;
    }

    // Force materials update across the WebGL pipeline
    if (terrainObj.mesh.material.uniforms && terrainObj.mesh.material.uniforms.uShadingMode) {
        terrainObj.mesh.material.uniforms.uShadingMode.value = modeIndex;
        terrainObj.mesh.material.needsUpdate = true;
    }
}

// ---------------------------------------------------------------------------
// INTERNAL UTILITIES
// ---------------------------------------------------------------------------

/** Return the first registered raster object, or null. */
function _findTerrainObject() {
    for (const id in sceneObjects) {
        if (sceneObjects[id].type === "raster") return sceneObjects[id];
    }
    return null;
}

/**
 * Reconstruct a minimal data-like object from cached globals.
 * Used when rebuilding the block base after a thickness change.
 */
function _buildDataFromCache() {
    const h = originalDEMValues.length;
    const w = originalDEMValues[0].length;
    return {
        width: w,
        height: h,
        z_values: originalDEMValues,
        z_min: elevationStats.min,
        z_max: elevationStats.max,
        nodata_value: null,
        x_coords: Array.from({ length: w }, (_, i) => (i / (w - 1) - 0.5) * cachedModelWidth),
        y_coords: Array.from({ length: h }, (_, j) => (0.5 - j / (h - 1)) * cachedModelHeight),
    };
}

function _computeMaxDim(data) {
    const mW = Math.abs(data.x_coords[data.x_coords.length - 1] - data.x_coords[0]);
    const mH = Math.abs(data.y_coords[data.y_coords.length - 1] - data.y_coords[0]);
    return Math.max(mW, mH);
}

function _updateStatus(message) {
    const el = document.getElementById("status-overlay");
    if (el) el.innerText = message;
}


// Dynamically build a floating scientific status overlay in the viewport
function _createCameraOverlay(container) {
    const overlay = document.createElement("div");
    overlay.id = "camera-overlay";
    overlay.innerText = "Projection: Perspective (Scenic View)";
    Object.assign(overlay.style, {
        position: "absolute",
        top: "10px",
        right: "10px",
        color: "#ddd",
        fontSize: "11px",
        pointerEvents: "none",
        background: "rgba(0, 0, 0, 0.6)",
        padding: "5px 8px",
        borderRadius: "4px",
        fontFamily: "monospace",
        zIndex: "100"
    });
    container.appendChild(overlay);
}

function _setCameraProjection(mode) {
    const container = document.getElementById("viewport-container");
    const aspect = container.clientWidth / container.clientHeight;

    // Clone the rotation target coordinates to keep focus centered smoothly
    const currentTarget = controls.target.clone();

    if (mode === "ortho") {
        const frustumSize = 120.0;
        orthographicCamera.left = (frustumSize * aspect) / -2;
        orthographicCamera.right = (frustumSize * aspect) / 2;
        orthographicCamera.top = frustumSize / 2;
        orthographicCamera.bottom = (frustumSize) / -2;
        orthographicCamera.updateProjectionMatrix();

        // Match viewpoint vectors from the perspective camera
        orthographicCamera.position.copy(camera.position);
        orthographicCamera.rotation.copy(camera.rotation);

        camera = orthographicCamera;
        _updateCameraOverlayText("Projection: Orthographic (Scientific Scale)");
    } else {
        perspectiveCamera.position.copy(camera.position);
        perspectiveCamera.rotation.copy(camera.rotation);

        camera = perspectiveCamera;
        _updateCameraOverlayText("Projection: Perspective (Scenic View)");
    }

    // Rebind the orbital viewport controller to the active camera object
    controls.object = camera;
    controls.target.copy(currentTarget);
    controls.update();
}

function _updateCameraOverlayText(message) {
    const el = document.getElementById("camera-overlay");
    if (el) el.innerText = message;
}


/**
 * Dynamically redraw the color scale legend widget to match active symbology and bounds.
 */
function _updateLegendWidget() {
    const legend = document.getElementById("legend-overlay");
    if (!legend || !originalDEMValues) {
        if (legend) legend.style.display = "none";
        return;
    }

    // Hide legend if solid color mode is active OR if the user unchecked it in Python UI
    if (colorMode === "solid" || colorMode === "natural" || !legendVisibleUserPref) {
        legend.style.display = "none";
        return;
    }

    legend.style.display = "block";
    const canvas = document.getElementById("legend-canvas");
    const labelsDiv = document.getElementById("legend-labels");
    labelsDiv.innerHTML = ""; // Clear old ticks

    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    const minZ = colorBoundsAuto ? elevationStats.min : colorBoundsMin;
    const maxZ = colorBoundsAuto ? elevationStats.max : colorBoundsMax;

    // ── CASE 1: CONTINUOUS COLORMAP MODE ─────────────────────────────────
    if (colorMode === "colormap") {
        const ramp = (typeof ACTIVE_SCENE_DATA !== "undefined" && ACTIVE_SCENE_DATA.style && ACTIVE_SCENE_DATA.style.active_ramp)
            ? ACTIVE_SCENE_DATA.style.active_ramp
            : (COLORMAPS[currentColormapName] || COLORMAPS.viridis);

        if (!ramp || ramp.length === 0) return;

        // Draw vertical linear gradient (top is maxZ, bottom is minZ)
        const grad = ctx.createLinearGradient(0, 0, 0, h);
        ramp.forEach(stop => {
            const pos = currentColormapReverse ? stop.pos : (1.0 - stop.pos);
            grad.addColorStop(pos, `rgb(${Math.round(stop.r * 255)}, ${Math.round(stop.g * 255)}, ${Math.round(stop.b * 255)})`);
        });

        ctx.fillStyle = grad;
        ctx.fillRect(0, 0, w, h);

        // Generate standard publishing ticks (Max, Mid-point, Min)
        _createLegendTickLabel(labelsDiv, `${maxZ.toFixed(1)}m`, 0);
        _createLegendTickLabel(labelsDiv, `${((minZ + maxZ) / 2).toFixed(1)}m`, 50);
        _createLegendTickLabel(labelsDiv, `${minZ.toFixed(1)}m`, 100);
    }
    // ── CASE 2: CLASSIFIED INTERVALS MODE ────────────────────────────────
    else if (colorMode === "classified" && currentClassColors.length > 0) {
        const numClasses = currentClassColors.length;
        const blockHeight = h / numClasses;

        // Draw discrete class color blocks
        for (let i = 0; i < numClasses; i++) {
            // Reverse order to draw highest values at the top of the canvas
            const idx = numClasses - 1 - i;
            ctx.fillStyle = currentClassColors[idx];
            ctx.fillRect(0, i * blockHeight, w, blockHeight);
        }

        // Generate discrete ticks for boundaries
        _createLegendTickLabel(labelsDiv, `${maxZ.toFixed(1)}m`, 0);
        for (let i = 0; i < currentClassBounds.length; i++) {
            const boundVal = currentClassBounds[currentClassBounds.length - 1 - i];
            const percent = ((i + 1) / numClasses) * 100;
            _createLegendTickLabel(labelsDiv, `${boundVal.toFixed(1)}m`, percent);
        }
        _createLegendTickLabel(labelsDiv, `${minZ.toFixed(1)}m`, 100);
    }
}

/**
 * Generate and position an absolute-positioned tick label alongside the canvas.
 */
function _createLegendTickLabel(parent, text, topPercent) {
    const tick = document.createElement("div");
    tick.className = "legend-tick";
    tick.innerText = text;
    tick.style.top = `${topPercent}%`;
    parent.appendChild(tick);
}



/**
 * Convert float RGB values [0..1] to a standard hex color string (#RRGGBB)
 * to ensure 100% compatibility with legacy vector parsers like Adobe Illustrator.
 */
function _rgbToHex(r, g, b) {
    const toHex = c => {
        const hex = Math.round(c * 255).toString(16);
        return hex.length === 1 ? "0" + hex : hex;
    };
    return "#" + toHex(r) + toHex(g) + toHex(b);
}

/**
 * Render and capture the 3D viewport.
 * Uses HTML5 2D canvas drawing to bake the legend directly into screenshot figures.
 */
function _exportViewport(format, dpi) {
    const container = document.getElementById("viewport-container");
    const width = container.clientWidth;
    const height = container.clientHeight;

    // 1. Temporarily hide layout helpers during rendering
    const gridObj = sceneObjects["scene_grid"];
    let tempGridVisible = false;
    if (gridObj) {
        tempGridVisible = gridObj.mesh.visible;
        gridObj.mesh.visible = false;
    }
    const tempAxesVisible = axesSceneVisible;
    axesSceneVisible = false;

    // 2. Resolve background clearing
    if (format === "jpg" || format === "jpeg") {
        renderer.setClearColor(0xffffff, 1.0);
    }

    // 3. Render the clean WebGL frame at the requested resolution: the
    //    drawing buffer is enlarged by dpi / 96 (the screen reference),
    //    within the 8192-px WebGL limit and at most 4×. The resolution
    //    choice was ignored before (exports came out at screen size).
    const exportScale = Math.max(1, Math.min(4, (dpi || 96) / 96, 8192 / Math.max(width, height)));
    const screenRatio = renderer.getPixelRatio();
    renderer.setPixelRatio(exportScale);
    renderer.setSize(width, height, false);
    renderer.setViewport(0, 0, width, height);
    renderer.setScissor(0, 0, width, height);
    renderer.setScissorTest(false);
    renderer.clear();
    renderer.render(scene, camera);

    // 4. Capture the raw WebGL canvas (read before the next frame clears it)
    const webglCanvas = document.createElement("canvas");
    webglCanvas.width = renderer.domElement.width;
    webglCanvas.height = renderer.domElement.height;
    webglCanvas.getContext("2d").drawImage(renderer.domElement, 0, 0);

    // 5. Restore the screen resolution and helpers. The animation loop is
    //    still running: calling _animate() again here started a second
    //    loop at every export, slowing QGIS down a little more each time.
    renderer.setPixelRatio(screenRatio);
    renderer.setSize(width, height, false);
    if (gridObj) gridObj.mesh.visible = tempGridVisible;
    axesSceneVisible = tempAxesVisible;
    renderer.setClearColor(0x000000, 0.0);

    // ── CASE A: VECTOR SVG EXPORT (For Adobe Illustrator) ────────────────
    if (format === "svg") {
        const terrainDataURL = webglCanvas.toDataURL("image/png");
        let legendSVGGroup = "";
        let gradientStops = "";

        if (legendVisibleUserPref && colorMode !== "solid" && colorMode !== "natural" && originalDEMValues) {
            const minZ = colorBoundsAuto ? elevationStats.min : colorBoundsMin;
            const maxZ = colorBoundsAuto ? elevationStats.max : colorBoundsMax;
            const legendX = 25;
            const legendY = 25;
            const legendW = 120;
            const legendH = 240;

            let colorBarElement = "";
            let labelsElement = "";

            // ── CASE 1: CONTINUOUS GRADIENT BAR ───────────────────────────
            if (colorMode === "colormap") {
                const ramp = (typeof ACTIVE_SCENE_DATA !== "undefined" && ACTIVE_SCENE_DATA.style && ACTIVE_SCENE_DATA.style.active_ramp)
                    ? ACTIVE_SCENE_DATA.style.active_ramp
                    : (COLORMAPS[currentColormapName] || COLORMAPS.viridis);

                const sortedStops = [];
                ramp.forEach(stop => {
                    const pos = currentColormapReverse ? stop.pos : (1.0 - stop.pos);
                    const hexColor = _rgbToHex(stop.r, stop.g, stop.b);
                    sortedStops.push({ offset: pos, color: hexColor });
                });

                // Sort stops strictly in ascending order (0.0 to 1.0) for Adobe Illustrator compliance
                sortedStops.sort((a, b) => a.offset - b.offset);

                // Compile XML stops using clean decimal offsets
                sortedStops.forEach(stop => {
                    gradientStops += `        <stop offset="${stop.offset.toFixed(3)}" stop-color="${stop.color}" />\n`;
                });

                colorBarElement = `<rect x="12" y="30" width="22" height="180" fill="url(#legend-grad)" stroke="#555" stroke-width="1" />`;
                labelsElement += `            <text x="0" y="0" fill="#f0f0f0" font-family="monospace" font-size="10" alignment-baseline="middle">${maxZ.toFixed(1)}m</text>\n`;
                labelsElement += `            <text x="0" y="90" fill="#f0f0f0" font-family="monospace" font-size="10" alignment-baseline="middle">${((minZ + maxZ) / 2).toFixed(1)}m</text>\n`;
                labelsElement += `            <text x="0" y="180" fill="#f0f0f0" font-family="monospace" font-size="10" alignment-baseline="middle">${minZ.toFixed(1)}m</text>\n`;
            }
            // ── CASE 2: CLASSIFIED BRACKETS BLOCKS ────────────────────────
            else if (colorMode === "classified" && currentClassColors.length > 0) {
                const numClasses = currentClassColors.length;
                const blockHeight = 180 / numClasses;
                let blocks = "";

                for (let i = 0; i < numClasses; i++) {
                    const idx = numClasses - 1 - i;
                    blocks += `        <rect x="12" y="${30 + i * blockHeight}" width="22" height="${blockHeight}" fill="${currentClassColors[idx]}" stroke="none" />\n`;
                }
                // Outer boundary stroke around the blocks
                blocks += `        <rect x="12" y="30" width="22" height="180" fill="none" stroke="#555" stroke-width="1" />`;
                colorBarElement = blocks;

                // Ticks for classified bounds
                labelsElement += `            <text x="0" y="0" fill="#f0f0f0" font-family="monospace" font-size="10" alignment-baseline="middle">${maxZ.toFixed(1)}m</text>\n`;
                for (let i = 0; i < currentClassBounds.length; i++) {
                    const boundVal = currentClassBounds[currentClassBounds.length - 1 - i];
                    const yPos = ((i + 1) / numClasses) * 180;
                    labelsElement += `            <text x="0" y="${yPos}" fill="#f0f0f0" font-family="monospace" font-size="10" alignment-baseline="middle">${boundVal.toFixed(1)}m</text>\n`;
                }
                labelsElement += `            <text x="0" y="180" fill="#f0f0f0" font-family="monospace" font-size="10" alignment-baseline="middle">${minZ.toFixed(1)}m</text>\n`;
            }

            // Generate clean, structured vector tags for Adobe Illustrator
            legendSVGGroup = `
    <g id="vector-colorbar" transform="translate(${legendX}, ${legendY})">
        <rect width="${legendW}" height="${legendH}" rx="6" fill="#141414" fill-opacity="0.45" stroke="rgba(255,255,255,0.1)" stroke-width="1" />
        <text x="${legendW / 2}" y="20" fill="#f0f0f0" font-family="monospace" font-size="11" font-weight="bold" text-anchor="middle">Elevation (m)</text>
        ${colorBarElement}
        <g id="legend-ticks" transform="translate(42, 35)">
${labelsElement}        </g>
    </g>`;
        }

        // Compile the SVG using decimal coordinates (x1=0 y1=0 x2=0 y2=1) for standard gradient units
        const svgContent = `<?xml version="1.0" encoding="utf-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${width}" height="${height}">
    <defs>
        <linearGradient id="legend-grad" x1="0" y1="0" x2="0" y2="1">
${gradientStops}        </linearGradient>
    </defs>
    <image xlink:href="${terrainDataURL}" width="${width}" height="${height}" x="0" y="0" />
    ${legendSVGGroup}
</svg>`;

        if (typeof bridge !== "undefined") {
            const svgEncoded = encodeURIComponent(svgContent);

            // Chunk the SVG payload to bypass QWebChannel transport limits (1MB chunks)
            const chunkSize = 1024 * 1024;
            const totalChunks = Math.ceil(svgEncoded.length / chunkSize);

            for (let i = 0; i < totalChunks; i++) {
                const start = i * chunkSize;
                const end = Math.min(start + chunkSize, svgEncoded.length);
                const chunk = svgEncoded.substring(start, end);

                const chunkDataURL = "data:image/svg+xml;chunk;index=" + i + ";total=" + totalChunks + "," + chunk;
                bridge.receive_export(chunkDataURL);
            }
        } else {
            console.warn("[RockMorph] QWebChannel bridge offline. Export aborted.");
        }
        return;
    }

    // ── CASE B: RASTER EXPORTS (PNG / JPG / PDF) ─────────────────────────
    // We create a temporary 2D canvas to merge WebGL and the Legend perfectly
    const mergeCanvas = document.createElement("canvas");
    mergeCanvas.width = webglCanvas.width;
    mergeCanvas.height = webglCanvas.height;
    const mergeCtx = mergeCanvas.getContext("2d");

    // Step 1: Draw the 3D terrain as base layer, then work in screen
    // coordinates scaled to the export resolution so the legend keeps its
    // layout and stays sharp.
    mergeCtx.drawImage(webglCanvas, 0, 0);
    mergeCtx.scale(webglCanvas.width / width, webglCanvas.height / height);

    // Step 2: Overlay the colorbar legend natively on top using Canvas 2D API
    if (legendVisibleUserPref && colorMode !== "solid" && colorMode !== "natural" && originalDEMValues) {
        const minZ = colorBoundsAuto ? elevationStats.min : colorBoundsMin;
        const maxZ = colorBoundsAuto ? elevationStats.max : colorBoundsMax;

        const legendX = 25;
        const legendY = 25;
        const legendW = 110;
        const legendH = 240;

        // Draw translucent container
        mergeCtx.fillStyle = "rgba(20, 20, 20, 0.45)";
        mergeCtx.beginPath();
        if (mergeCtx.roundRect) {
            mergeCtx.roundRect(legendX, legendY, legendW, legendH, 6);
        } else {
            mergeCtx.rect(legendX, legendY, legendW, legendH);
        }
        mergeCtx.fill();
        mergeCtx.strokeStyle = "rgba(255, 255, 255, 0.1)";
        mergeCtx.lineWidth = 1;
        mergeCtx.stroke();

        // Draw Title text
        mergeCtx.fillStyle = "#f0f0f0";
        mergeCtx.font = "bold 11px Courier New, Courier, monospace";
        mergeCtx.textAlign = "center";
        mergeCtx.textBaseline = "alphabetic";
        mergeCtx.fillText("Elevation (m)", legendX + legendW / 2, legendY + 20);

        // Draw Colorbar
        const barX = legendX + 12;
        const barY = legendY + 30;
        const barW = 22;
        const barH = 180;

        if (colorMode === "colormap") {
            const ramp = (typeof ACTIVE_SCENE_DATA !== "undefined" && ACTIVE_SCENE_DATA.style && ACTIVE_SCENE_DATA.style.active_ramp)
                ? ACTIVE_SCENE_DATA.style.active_ramp
                : (COLORMAPS[currentColormapName] || COLORMAPS.viridis);

            const grad = mergeCtx.createLinearGradient(0, barY, 0, barY + barH);
            ramp.forEach(stop => {
                const pos = currentColormapReverse ? stop.pos : (1.0 - stop.pos);
                grad.addColorStop(pos, `rgb(${Math.round(stop.r * 255)}, ${Math.round(stop.g * 255)}, ${Math.round(stop.b * 255)})`);
            });
            mergeCtx.fillStyle = grad;
            mergeCtx.fillRect(barX, barY, barW, barH);
        } else if (colorMode === "classified" && currentClassColors.length > 0) {
            const numClasses = currentClassColors.length;
            const blockHeight = barH / numClasses;
            for (let i = 0; i < numClasses; i++) {
                const idx = numClasses - 1 - i;
                mergeCtx.fillStyle = currentClassColors[idx];
                mergeCtx.fillRect(barX, barY + i * blockHeight, barW, blockHeight);
            }
        }

        // Draw colorbar border
        mergeCtx.strokeStyle = "#555";
        mergeCtx.strokeRect(barX, barY, barW, barH);

        // Draw text labels and ticks
        mergeCtx.fillStyle = "#f0f0f0";
        mergeCtx.font = "10px Courier New, Courier, monospace";
        mergeCtx.textAlign = "left";
        mergeCtx.textBaseline = "middle";

        const labelX = barX + barW + 8;
        if (colorMode === "colormap") {
            mergeCtx.fillText(`${maxZ.toFixed(1)}m`, labelX, barY);
            mergeCtx.fillText(`${((minZ + maxZ) / 2).toFixed(1)}m`, labelX, barY + barH / 2);
            mergeCtx.fillText(`${minZ.toFixed(1)}m`, labelX, barY + barH);
        } else if (colorMode === "classified") {
            const numClasses = currentClassColors.length;
            mergeCtx.fillText(`${maxZ.toFixed(1)}m`, labelX, barY);
            for (let i = 0; i < currentClassBounds.length; i++) {
                const boundVal = currentClassBounds[currentClassBounds.length - 1 - i];
                const yPos = barY + ((i + 1) / numClasses) * barH;
                mergeCtx.fillText(`${boundVal.toFixed(1)}m`, labelX, yPos);
            }
            mergeCtx.fillText(`${minZ.toFixed(1)}m`, labelX, barY + barH);
        }
    }

    // Capture the final merged canvas and return the clean dataURL
    let mimeType = "image/png";
    if (format === "jpg" || format === "jpeg") {
        mimeType = "image/jpeg";
    }
    const finalDataURL = mergeCanvas.toDataURL(mimeType, 0.95);

    if (typeof bridge !== "undefined") {
        bridge.receive_export(finalDataURL);
    } else {
        console.warn("[RockMorph] QWebChannel bridge offline. Export aborted.");
    }
}

// ── EXPORTATEUR STL HAUTE PERFORMANCE ────────────────────────────────
function _exportSTL() {
    // Array pushes avoid continuous string allocations and run 100x faster
    const lines = ["solid rockmorph_terrain"];

    for (const id in sceneObjects) {
        const obj = sceneObjects[id];
        if (!obj || !obj.mesh || !obj.visible) continue;
        if (obj.type !== "raster" && obj.type !== "walls" && obj.type !== "sole") continue;

        const mesh = obj.mesh;
        const geometry = mesh.geometry;
        if (!geometry) continue;

        const positionAttr = geometry.attributes.position;
        const indexAttr = geometry.index;
        if (!positionAttr) continue;

        const positions = positionAttr.array;

        if (indexAttr) {
            const indices = indexAttr.array;
            for (let i = 0; i < indices.length; i += 3) {
                const i1 = indices[i], i2 = indices[i + 1], i3 = indices[i + 2];

                const x1 = positions[i1 * 3], y1 = positions[i1 * 3 + 1], z1 = positions[i1 * 3 + 2];
                const x2 = positions[i2 * 3], y2 = positions[i2 * 3 + 1], z2 = positions[i2 * 3 + 2];
                const x3 = positions[i3 * 3], y3 = positions[i3 * 3 + 1], z3 = positions[i3 * 3 + 2];

                const ux = x2 - x1, uy = y2 - y1, uz = z2 - z1;
                const vx = x3 - x1, vy = y3 - y1, vz = z3 - z1;
                const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
                const len = Math.hypot(nx, ny, nz) || 1;

                lines.push(`  facet normal ${nx / len} ${ny / len} ${nz / len}`);
                lines.push("    outer loop");
                lines.push(`      vertex ${x1} ${y1} ${z1}`);
                lines.push(`      vertex ${x2} ${y2} ${z2}`);
                lines.push(`      vertex ${x3} ${y3} ${z3}`);
                lines.push("    endloop");
                lines.push("  endfacet");
            }
        } else {
            for (let i = 0; i < positionAttr.count; i += 3) {
                const x1 = positions[i * 3], y1 = positions[i * 3 + 1], z1 = positions[i * 3 + 2];
                const x2 = positions[i * 3 + 3], y2 = positions[i * 3 + 4], z2 = positions[i * 3 + 5];
                const x3 = positions[i * 3 + 6], y3 = positions[i * 3 + 7], z3 = positions[i * 3 + 8];

                const ux = x2 - x1, uy = y2 - y1, uz = z2 - z1;
                const vx = x3 - x1, vy = y3 - y1, vz = z3 - z1;
                const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
                const len = Math.hypot(nx, ny, nz) || 1;

                lines.push(`  facet normal ${nx / len} ${ny / len} ${nz / len}`);
                lines.push("    outer loop");
                lines.push(`      vertex ${x1} ${y1} ${z1}`);
                lines.push(`      vertex ${x2} ${y2} ${z2}`);
                lines.push(`      vertex ${x3} ${y3} ${z3}`);
                lines.push("    endloop");
                lines.push("  endfacet");
            }
        }
    }
    lines.push("endsolid rockmorph_terrain");

    // Perform a single, ultra-fast join
    const stl = lines.join("\n");

    if (typeof bridge !== "undefined") {
        // Chunk the STL text payload to bypass QWebChannel / Chromium IPC transport limits (usually 2MB)
        const chunkSize = 1024 * 1024; // 1 MB chunks
        const totalChunks = Math.ceil(stl.length / chunkSize);

        for (let i = 0; i < totalChunks; i++) {
            const start = i * chunkSize;
            const end = Math.min(start + chunkSize, stl.length);
            const chunk = stl.substring(start, end);

            // Encode the chunk metadata inside the custom data URL header
            const chunkDataURL = "data:model/stl;chunk;index=" + i + ";total=" + totalChunks + "," + chunk;
            bridge.receive_export(chunkDataURL);
        }
    } else {
        console.warn("[RockMorph] QWebChannel bridge offline. Export aborted.");
    }
}


// ── BOOT INITIALIZATION SÉCURISÉ (HTML AUTONOME ET LOCAL) ────────────
if (document.readyState === "complete" || document.readyState === "interactive") {
    _initStandaloneOnBoot();
} else {
    window.addEventListener("DOMContentLoaded", _initStandaloneOnBoot);
}

function _initStandaloneOnBoot() {
    initScene();

    // Check if we are running in standalone exported mode (QWebChannel absent)
    if (typeof ACTIVE_SCENE_DATA !== "undefined") {
        _buildTerrain(ACTIVE_SCENE_DATA.raster);

        if (ACTIVE_SCENE_DATA.vectors) {
            ACTIVE_SCENE_DATA.vectors.forEach(v => _buildVectorFeature(v));
        }

        const style = ACTIVE_SCENE_DATA.style;
        if (style) {
            _updateZScale(style.z_scale);
            _setObjectColor("block_base_walls", style.walls_color);
            _setObjectColor("block_base_sole", style.base_color);

            skyColorTop = new THREE.Color(style.sky_top);
            skyColorBottom = new THREE.Color(style.sky_bottom);
            _applySkyGradient();

            if (style.color_mode === "classified") {
                currentClassColors = style.class_colors;
                currentClassBounds = style.class_bounds;
                _applyClassifiedColors();
            } else if (style.color_mode === "solid") {
                _applySolidColor(style.solid_color);
            } else if (style.color_mode === "natural") {
                colorMode = "natural";
                _applyNaturalColors(style.natural_lines || {});
            } else {
                currentColormapName = style.colormap;
                currentColormapReverse = style.reverse_cmap;
                _applyColormap();
            }

            globalRoughness = style.roughness;
            globalSlopeContrast = style.slope_contrast;
            globalMultidirectional = style.multidirectional;
            if (style.realism) {
                processPythonCommand({ action: "update_realism", payload: style.realism });
            }
            updateSceneUniforms();
        }
    }
}