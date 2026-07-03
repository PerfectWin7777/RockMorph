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
    container.appendChild(renderer.domElement);

    // Orbit controls
    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.05;

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
    renderer.render(scene, camera);
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
                    const obj = sceneObjects[elementId];
                    if (obj) {
                        obj.mesh.visible = isVisible;
                        obj.visible = isVisible;
                    }
                }
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


    // 1. Identify and cleanly unregister any pre-existing raster in the scene
    for (const id in sceneObjects) {
        if (sceneObjects[id].type === "raster") {
            unregisterObject(id);
        }
    }

    // 2. Clear block elements and reset reference variables
    unregisterObject("block_base_walls");
    unregisterObject("block_base_sole");
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
            const c = _sampleRamp(COLORMAPS[currentColormapName] || COLORMAPS.viridis,
                currentColormapReverse ? 1 - t : t);
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
        }
    }
}

function _updateVectorGeometryZ(mesh, descriptor) {
    const posAttr = mesh.geometry.attributes.position;
    const positions = posAttr.array;
    
    let idx = 0;
    descriptor.vertices.forEach(([x, y, z]) => {
        positions[idx + 2] = (z - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;
        idx += 3;
    });
    posAttr.needsUpdate = true;
    mesh.geometry.computeBoundingSphere();
}

function _updateCurtainGeometryZ(mesh, descriptor) {
    const posAttr = mesh.geometry.attributes.position;
    const positions = posAttr.array;
    
    let idx = 0;
    descriptor.vertices.forEach(([x, y, z]) => {
        const zTop = (z - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;
        const zBottom = zTop - (descriptor.extrude_depth || 0.0) * scaleFactor;
        positions[idx + 2] = zTop;
        positions[idx + 5] = zBottom;
        idx += 6;
    });
    posAttr.needsUpdate = true;
    mesh.geometry.computeVertexNormals();
    mesh.geometry.computeBoundingSphere();
}

function _updatePointMarkerZ(mesh, descriptor) {
    const [x, y, z] = descriptor.vertices[0];
    const zDraped = (z - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor;
    const floatOffset = modelMaxDim * 0.005; 
    mesh.position.z = zDraped + floatOffset;
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
 */
function _sampleRamp(stops, t) {
    t = Math.max(0, Math.min(1, t));
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

    const ramp = COLORMAPS[currentColormapName] || COLORMAPS.viridis;
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

    // Handle point geometry (e.g. knickpoints, sample markers)
    if (descriptor.geom_type === "point") {
        _buildPointMarker(descriptor);
        return;
    }

    if (descriptor.vertices.length < 2) return;

    const positions = [];
    descriptor.vertices.forEach(([x, y, z]) => {
        positions.push(
            (x - spatialOffsets.x) * scaleFactor,
            (y - spatialOffsets.y) * scaleFactor,
            (z - spatialOffsets.z) * baseExaggeration * currentZScale * scaleFactor
        );
    });

    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));

    const mat = new THREE.LineBasicMaterial({
        color: descriptor.color || "#3498db",
        linewidth: 2
    });
    const line = new THREE.Line(geo, mat);
    scene.add(line);

    // Register the vector object with its unscaled raw data for scale synchronization
    sceneObjects[descriptor.element_id] = {
        mesh: line,
        type: "vector",
        visible: true,
        descriptor: descriptor
    };

    // If an extrusion depth is present, construct a structural geological plane
    if (descriptor.extrude_depth && descriptor.extrude_depth > 0) {
        _buildExtrudedCurtain(descriptor, positions);
    }
}

function _buildExtrudedCurtain(descriptor, topPositions) {
    const curtainPositions = [];
    for (let i = 0; i < topPositions.length; i += 3) {
        const x = topPositions[i];
        const y = topPositions[i + 1];
        const zTop = topPositions[i + 2];
        curtainPositions.push(x, y, zTop);
        curtainPositions.push(x, y, zTop - (descriptor.extrude_depth || 0.0) * scaleFactor);
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

    const mat = createStructuralMaterial(new THREE.Color(descriptor.color || "#e74c3c"));
    const mesh = new THREE.Mesh(geo, mat);
    scene.add(mesh);

    // Register structural curtain with its descriptor for dynamic Z-scaling
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
    const floatOffset = modelMaxDim * 0.005; // Offset to prevent clipping

    const geo = new THREE.SphereGeometry(modelMaxDim * 0.015, 12, 12);
    const mat = createStructuralMaterial(new THREE.Color(descriptor.color || "#f1c40f"));
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

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

window.onload = initScene;