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
// 1. TERRAIN SHADERS (With Vertex Colors for Colormaps)
// ---------------------------------------------------------------------------

const terrainVertexShader = `
    attribute vec3 color;
    varying vec3 vNormal;
    varying vec3 vColor;
    varying vec3 vWorldNormal;

    void main() {
        vNormal = normalize(normalMatrix * normal);
        vWorldNormal = normalize(normal);
        vColor = color;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
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

    varying vec3 vNormal;
    varying vec3 vWorldNormal;
    varying vec3 vColor;

    void main() {
        vec3 normal = normalize(vNormal);
        vec3 worldNormal = normalize(vWorldNormal);
        
        // Hemisphere Ambient Light (Cool sky / Warm ground reflection)
        vec3 skyColor = vec3(0.55, 0.68, 0.85);
        vec3 groundColor = vec3(0.20, 0.17, 0.13);
        float hemiMix = worldNormal.z * 0.5 + 0.5;
        vec3 ambient = mix(groundColor, skyColor, hemiMix) * uAmbientIntensity;
        
        // Multidirectional Shading (GIS 4-Axis Mode) vs Single-Directional Shading
        vec3 L0 = normalize(uSunDirection);
        float diffuseTerm = 0.0;

        if (uMultidirectional) {
            vec3 L1 = vec3(-L0.y, L0.x, L0.z);
            vec3 L2 = vec3(-L0.x, -L0.y, L0.z);
            vec3 L3 = vec3(L0.y, -L0.x, L0.z);

            float d0 = max(dot(normal, L0), 0.0);
            float d1 = max(dot(normal, L1), 0.0);
            float d2 = max(dot(normal, L2), 0.0);
            float d3 = max(dot(normal, L3), 0.0);

            diffuseTerm = d0 * 0.4 + d1 * 0.2 + d2 * 0.2 + d3 * 0.2;
        } else {
            diffuseTerm = max(dot(normal, L0), 0.0);
        }

        // Apply Oren-Nayar retro-reflection factor
        float roughness2 = uRoughness * uRoughness;
        float orenNayarFactor = 1.0 - 0.5 * (roughness2 / (roughness2 + 0.33));
        float directDiffuse = diffuseTerm * orenNayarFactor;
        
        // 4. Slope shading accentuation (darken steep incisions based on normal Z tilt)
        // High exponent scale (15.0) ensures high reactivity on moderate topography
        float slopeFactor = pow(clamp(worldNormal.z, 0.0, 1.0), 1.0 + uSlopeContrast * 15.0);
        
        vec3 finalLighting = (ambient * slopeFactor) + (uSunColor * directDiffuse * uSunIntensity);
        vec3 baseTerrainColor = uUseVertexColors ? vColor : uSolidColor;
        gl_FragColor = vec4(baseTerrainColor * finalLighting, 1.0);
    }
`;

function createCustomMaterial(useVertexColors, solidColorObj) {
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
            uMultidirectional: { value: globalMultidirectional }
        },
        side: THREE.DoubleSide
    });
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

    varying vec3 vNormal;
    varying vec3 vWorldNormal;

    void main() {
        vec3 normal = normalize(vNormal);
        vec3 worldNormal = normalize(vWorldNormal);
        
        vec3 L = normalize(uSunDirection);
        float NdotL = max(dot(normal, L), 0.0);
        
        // Pure hemispherical ambient fill using static world coordinates (Up is Z)
        vec3 skyColor = vec3(0.55, 0.68, 0.85);
        vec3 groundColor = vec3(0.20, 0.17, 0.13);
        float hemiMix = worldNormal.z * 0.5 + 0.5;
        vec3 ambient = mix(groundColor, skyColor, hemiMix) * uAmbientIntensity;
        
        vec3 finalLighting = ambient + (uSunColor * NdotL * uSunIntensity);
        gl_FragColor = vec4(uSolidColor * finalLighting, 1.0);
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
            uSolidColor: { value: solidColorObj || new THREE.Color(0xffffff) }
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
    console.log("[JS Receive Command] Action reçue : " + command.action + " | Données : " + JSON.stringify(command.payload));
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

    // ShaderMaterial customized with standard parameters
    const material = createCustomMaterial(true, null);

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
    } else {
        _applyColormap();
    }

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
    }

    _updateLegendWidget();
}

/** Apply a flat solid color to the terrain (disables vertex colors). */
function _applySolidColor(hexColor) {
    const terrainObj = _findTerrainObject();
    if (!terrainObj) return;
    const uniforms = terrainObj.mesh.material.uniforms;
    if (uniforms) {
        uniforms.uUseVertexColors.value = false;
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

    // 1. Evaluate Dynamic Color (Fixed vs Attribute-Mapped) [No Hardcoding]
    let featureColor = descriptor.color || "#3498db";

    if (descriptor.color_styling === "attribute" && descriptor.attribute_values && descriptor.attribute_bounds) {
        const val = descriptor.attribute_values[0];
        const minVal = descriptor.attribute_bounds.min;
        const maxVal = descriptor.attribute_bounds.max;
        const norm = (val - minVal) / (maxVal - minVal);

        // Sample custom palette
        const rampName = descriptor.vector_colormap || "terrain";
        // Safely fallback to the main terrain active ramp if global colormaps are missing
        const ramp = COLORMAPS[rampName] || COLORMAPS.terrain || COLORMAPS.viridis || (typeof ACTIVE_SCENE_DATA !== "undefined" ? ACTIVE_SCENE_DATA.style.active_ramp : null);
        const sampled = _sampleRamp(ramp, norm);

        featureColor = "#" +
            Math.round(sampled.r * 255).toString(16).padStart(2, "0") +
            Math.round(sampled.g * 255).toString(16).padStart(2, "0") +
            Math.round(sampled.b * 255).toString(16).padStart(2, "0");
    }

    descriptor.resolved_color = featureColor; // Cache for subsequent sub-mesh generations

    // Route geometry types
    if (descriptor.geom_type === "point") {
        _buildPointMarker(descriptor);
        return;
    }
    if (descriptor.geom_type === "filled") {
        _buildFilledPolygon(descriptor);
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
        linewidth: descriptor.line_width || 2
    });
    const line = new THREE.Line(geo, mat);
    scene.add(line);

    sceneObjects[descriptor.element_id] = {
        mesh: line,
        type: "vector",
        visible: true,
        descriptor: descriptor
    };

    if (descriptor.extrude_depth && descriptor.extrude_depth > 0) {
        _buildExtrudedCurtain(descriptor, positions);
    }
}

function _buildExtrudedCurtain(descriptor, topPositions) {
    const curtainPositions = [];
    // Scale real-world depth meters to match exaggerated terrain vertical axis [Fix 3]
    const scaledDepth = (descriptor.extrude_depth || 0.0) * baseExaggeration * currentZScale * scaleFactor;

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

function _buildPointMarker(descriptor) {
    const [x, y, z] = descriptor.vertices[0];
    const zDraped = (z - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;
    const floatOffset = modelMaxDim * 0.005;

    // Apply point scale multiplier read from UI
    const sizeMultiplier = descriptor.point_marker_size || 1.0;
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

function _buildFilledPolygon(descriptor) {
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

    const color = new THREE.Color(descriptor.resolved_color);
    const mat = new THREE.MeshBasicMaterial({
        color: color,
        transparent: true,
        opacity: descriptor.polygon_opacity || 0.5,
        side: THREE.DoubleSide,
        depthWrite: false // Prevents overlay sorting issues
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
        desc.vector_colormap = style.vector_colormap;

        let resolvedColor = style.fixed_color || "#3498db";
        if (style.color_styling === "attribute" && desc.attribute_values && desc.attribute_bounds) {
            const val = desc.attribute_values[0];
            const minVal = desc.attribute_bounds.min;
            const maxVal = desc.attribute_bounds.max;
            const norm = (val - minVal) / (maxVal - minVal);

            const ramp = COLORMAPS[style.vector_colormap] || COLORMAPS.terrain || COLORMAPS.viridis;
            const sampled = _sampleRamp(ramp, norm);

            resolvedColor = "#" +
                Math.round(sampled.r * 255).toString(16).padStart(2, "0") +
                Math.round(sampled.g * 255).toString(16).padStart(2, "0") +
                Math.round(sampled.b * 255).toString(16).padStart(2, "0");
        }

        desc.color = resolvedColor;

        if (obj.type === "vector") {
            if (mesh.material.color) {
                mesh.material.color.set(resolvedColor);
            }
            mesh.material.linewidth = style.line_width || 2;
            mesh.material.needsUpdate = true;
            _updateVectorGeometryZ(mesh, desc);
        }
        else if (obj.type === "curtain") {
            // Defensive Check: Update uniforms if using custom ShaderMaterial, color if basic
            if (mesh.material.uniforms && mesh.material.uniforms.uSolidColor) {
                mesh.material.uniforms.uSolidColor.value.set(resolvedColor);
            } else if (mesh.material.color) {
                mesh.material.color.set(resolvedColor);
            }
            mesh.material.needsUpdate = true;
            _updateCurtainGeometryZ(mesh, desc);
        }
        else if (obj.type === "filled_polygon") {
            if (mesh.material.color) {
                mesh.material.color.set(resolvedColor);
            }
            mesh.material.opacity = style.polygon_opacity || 0.5;
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
            const sizeMultiplier = style.point_marker_size || 1.0;
            mesh.scale.setScalar(sizeMultiplier);
            _updatePointMarkerZ(mesh, desc);
        }
    }
}

// ---------------------------------------------------------------------------
// SHADING MODE (legacy compatibility)
// ---------------------------------------------------------------------------

function _updateShadingMode(mode) {
    const terrainObj = _findTerrainObject();
    if (!terrainObj) return;
    const mat = terrainObj.mesh.material;
    if (mode === "wireframe") {
        mat.wireframe = true;
    } else {
        mat.wireframe = false;
        mat.flatShading = (mode === "flat");
        mat.needsUpdate = true;
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
    if (colorMode === "solid" || !legendVisibleUserPref) {
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

    // 3. Render the clean WebGL frame
    renderer.setViewport(0, 0, width, height);
    renderer.setScissor(0, 0, width, height);
    renderer.setScissorTest(false);
    renderer.clear();
    renderer.render(scene, camera);

    // 4. Capture the raw WebGL canvas
    const webglCanvas = renderer.domElement;

    // 5. Restore screen visibility helpers
    if (gridObj) gridObj.mesh.visible = tempGridVisible;
    axesSceneVisible = tempAxesVisible;
    renderer.setClearColor(0x000000, 0.0);
    _animate(); // Resume loop

    // ── CASE A: VECTOR SVG EXPORT (For Adobe Illustrator) ────────────────
    if (format === "svg") {
        const terrainDataURL = webglCanvas.toDataURL("image/png");
        let legendSVGGroup = "";
        let gradientStops = "";

        if (legendVisibleUserPref && colorMode !== "solid" && originalDEMValues) {
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
    mergeCanvas.width = width;
    mergeCanvas.height = height;
    const mergeCtx = mergeCanvas.getContext("2d");

    // Step 1: Draw the 3D terrain as base layer
    mergeCtx.drawImage(webglCanvas, 0, 0);

    // Step 2: Overlay the colorbar legend natively on top using Canvas 2D API
    if (legendVisibleUserPref && colorMode !== "solid" && originalDEMValues) {
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
            } else {
                currentColormapName = style.colormap;
                currentColormapReverse = style.reverse_cmap;
                _applyColormap();
            }

            globalRoughness = style.roughness;
            globalSlopeContrast = style.slope_contrast;
            globalMultidirectional = style.multidirectional;
            updateSceneUniforms();
        }
    }
}