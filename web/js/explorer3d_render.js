// web/js/explorer3d_render.js

/**
 * RockMorph 3D Explorer Render Engine.
 * Manages Three.js scene, lighting, camera controls, and process dynamic JSON commands.
 * Uses high-compatibility MeshPhongMaterial to prevent black shader bugs on older WebGL drivers.
 * 
 * Authors: RockMorph contributors
 */

let scene, camera, renderer, controls;
let terrainMesh = null;
let lateralWalls = [];
let basePlane = null;

// Spatial offset caching to prevent float32 floating-point precision jitter
let spatialOffsets = { x: 0, y: 0, z: 0 };

// Dynamic scaling states
let currentZScale = 1.5;
let originalDEMValues = null;
let elevationStats = { min: 0, max: 1 }; 
// Dynamic scaling and boundary mapping states
let baseExaggeration = 1.0;  // Calculated automatically on load
let wallVertexMappings = []; // Maps wall vertices to raw elevation values for synchronized scaling [1.13.2]

/**
 * Initialize the empty 3D Viewport.
 */
function initScene() {
    const container = document.getElementById('viewport-container');
    const width = container.clientWidth;
    const height = container.clientHeight;

    // 1. Scene setup
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x1a1a1a);

    // 2. Camera setup
    camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000000);
    camera.position.set(0, -500, 500);

    // 3. Renderer setup - WebGL 1.0 compatible
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setSize(width, height);
    container.appendChild(renderer.domElement);

    // 4. Interactive controls
    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.05;

    // 5. High-compatibility light setup
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.35);
    scene.add(ambientLight);

    const dirLight = new THREE.DirectionalLight(0xffffff, 0.75);
    dirLight.position.set(300, -300, 1000);
    scene.add(dirLight);

    const hemiLight = new THREE.HemisphereLight(0xffffff, 0x444444, 0.4);
    hemiLight.position.set(0, 0, 1000);
    scene.add(hemiLight);

    // Start rendering loop
    animate();

    window.addEventListener('resize', onWindowResize, false);
}

function animate() {
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
}

function onWindowResize() {
    const container = document.getElementById('viewport-container');
    camera.aspect = container.clientWidth / container.clientHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(container.clientWidth, container.clientHeight);
}

/**
 * Dynamic Command Dispatcher called from QGIS PyQt Dock Panel [2].
 */
function processPythonCommand(command) {
    if (!command || !command.action) return;

    const action = command.action;
    const payload = command.payload;

    try {
        switch (action) {
            case "set_main_raster":
                build3DTerrain(payload);
                break;
            case "update_z_scale":
                updateZScale(payload.scale);
                break;
            case "set_shading_mode":
                updateShadingMode(payload.mode);
                break;
            case "toggle_walls":
                toggleWallsVisibility(payload.visible);
                break;
            default:
                console.warn("[RockMorph 3D] Unknown command action:", action);
        }
    } catch (e) {
        console.error("[RockMorph 3D] Error executing action: " + action, e);
    }
}

/**
 * Interpolator for the Viridis scientific colormap.
 */
function getColorFromRamp(normalizedVal) {
    const t = Math.max(0.0, Math.min(1.0, normalizedVal));

    const stops = [
        { pos: 0.00, color: new THREE.Color(0x260054) }, // Dark purple
        { pos: 0.25, color: new THREE.Color(0x2d708e) }, // Blue/Teal
        { pos: 0.50, color: new THREE.Color(0x1f9e89) }, // Green
        { pos: 0.75, color: new THREE.Color(0x6ece58) }, // Light green
        { pos: 1.00, color: new THREE.Color(0xfde725) }  // Yellow
    ];

    for (let i = 0; i < stops.length - 1; i++) {
        if (t >= stops[i].pos && t <= stops[i + 1].pos) {
            const range = stops[i + 1].pos - stops[i].pos;
            const localT = (t - stops[i].pos) / range;
            const c = stops[i].color.clone();
            return c.lerp(stops[i + 1].color, localT);
        }
    }
    return stops[stops.length - 1].color;
}

/**
 * Reconstruct a 3D surface mesh using high-compatibility MeshPhongMaterial.
 */
function build3DTerrain(data) {
    console.log("[RockMorph 3D] Starting build3DTerrain process...");
    console.log("[RockMorph 3D] Input Metadata - Width:", data.width, "Height:", data.height);
    console.log("[RockMorph 3D] Elevation Stats - Min:", data.z_min, "Max:", data.z_max);

    const container = document.getElementById('viewport-container');
    const actualWidth = container.clientWidth;
    const actualHeight = container.clientHeight;

    if (actualWidth > 0 && actualHeight > 0) {
        renderer.setSize(actualWidth, actualHeight);
        camera.aspect = actualWidth / actualHeight;
        camera.updateProjectionMatrix();
    }

    if (terrainMesh) scene.remove(terrainMesh);

    const width = data.width;
    const height = data.height;

    // Cache spatial statistics and values
    spatialOffsets.x = (data.x_coords[0] + data.x_coords[data.x_coords.length - 1]) / 2;
    spatialOffsets.y = (data.y_coords[0] + data.y_coords[data.y_coords.length - 1]) / 2;
    spatialOffsets.z = data.z_min;

    elevationStats.min = data.z_min;
    elevationStats.max = data.z_max;

    originalDEMValues = data.z_values;

    // Create plane geometry
    const geometry = new THREE.PlaneGeometry(
        Math.abs(data.x_coords[data.x_coords.length - 1] - data.x_coords[0]),
        Math.abs(data.y_coords[data.y_coords.length - 1] - data.y_coords[0]),
        width - 1,
        height - 1
    );


    // ─── OPTIMIZED SINGLE-PASS GEOMETRY GENERATOR ─────────────────────
    // Processes vertices, colors, and cuts out NoData triangles in a single loop [1.13.2]
    const vertices = geometry.attributes.position.array;
    const colors = [];
    const indices = [];
    let vertexIndex = 0;

    const elevationRange = Math.max(1.0, data.z_max - data.z_min);
    let nullOrNanCount = 0;

    const isNoData = (r, c) => {
        const val = data.z_values[r][c];
        return val === null || val === undefined || isNaN(val) || val === data.nodata_value;
    };

    // Calculate the physical dimensions of the model [1.13.2]
    const modelWidth = Math.abs(data.x_coords[data.x_coords.length - 1] - data.x_coords[0]);
    const modelHeight = Math.abs(data.y_coords[data.y_coords.length - 1] - data.y_coords[0]);
    const maxDim = Math.max(modelWidth, modelHeight);

    // DYNAMIC ASPECT RATIO CALCULATION (CRITICAL FIX FOR SCAPE) [1.13.2]
    // We want a 1.0x scale slider value to represent exactly 15% of the total model size [1.13.2]
    baseExaggeration = (maxDim * 0.15) / elevationRange;

    for (let j = 0; j < height; j++) {
        for (let i = 0; i < width; i++) {
            const rawZ = data.z_values[j][i];
            let actualRawZ = rawZ;
            let actualZ = 0;

            if (rawZ === null || rawZ === undefined || isNaN(rawZ) || rawZ === data.nodata_value) {
                nullOrNanCount++;
                actualRawZ = data.z_min;
                actualZ = 0;
            } else {
                // Apply baseExaggeration to the vertices [1.13.2]
                actualZ = (rawZ - spatialOffsets.z) * baseExaggeration * currentZScale;
            }

            vertices[vertexIndex + 2] = actualZ;
            vertexIndex += 3;
            // 2. Generate elevation colors
            let normElevation = (actualRawZ - data.z_min) / elevationRange;
            if (isNaN(normElevation) || !isFinite(normElevation)) {
                normElevation = 0.0;
            }

            const rgbColor = getColorFromRamp(normElevation);
            colors.push(rgbColor.r, rgbColor.g, rgbColor.b);

            // 3. Dynamically build index buffer on-the-fly, skipping NoData cells [1.13.2]
            if (j < height - 1 && i < width - 1) {
                const vTL = j * width + i;
                const vTR = j * width + (i + 1);
                const vBL = (j + 1) * width + i;
                const vBR = (j + 1) * width + (i + 1);

                // Only compile indices if all 4 corners are valid [1.13.2]
                if (!isNoData(j, i) && !isNoData(j, i + 1) && !isNoData(j + 1, i) && !isNoData(j + 1, i + 1)) {
                    indices.push(vTL, vBL, vTR);
                    indices.push(vTR, vBL, vBR);
                }
            }
        }
    }

    // Bind the optimized indices list directly to WebGL
    geometry.setIndex(indices);
    // ──────────────────────────────────────────────────────────────────

    console.log("[RockMorph 3D] Processing complete. Total NoData/NaN vertices adjusted:", nullOrNanCount);

    geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
    geometry.computeVertexNormals();

    // ─── HIGH-COMPATIBILITY MATERIAL SWAP (PHONG SHADING) ───────────
    // Replaces standard PBR material to prevent black shader compilations on ANGLE/QGIS WebGL [1.13.2]
    const material = new THREE.MeshPhongMaterial({
        vertexColors: true, // Read the interpolated colors buffer [1.13.2]
        shininess: 15,      // Smooth, realistic highlight reflections [1.13.2]
        specular: new THREE.Color(0x111111),
        flatShading: false,
        side: THREE.DoubleSide
    });
    // ──────────────────────────────────────────────────────────────────

    terrainMesh = new THREE.Mesh(geometry, material);
    scene.add(terrainMesh);

    // Dynamic Camera Adaptation [1.13.2]
    // const modelWidth = Math.abs(data.x_coords[data.x_coords.length - 1] - data.x_coords[0]);
    // const modelHeight = Math.abs(data.y_coords[data.y_coords.length - 1] - data.y_coords[0]);
    // const maxDim = Math.max(modelWidth, modelHeight);

    camera.near = maxDim * 0.01;
    camera.far = maxDim * 15.0;
    camera.updateProjectionMatrix();

    controls.target.set(0, 0, (data.z_max - data.z_min) * currentZScale / 2);
    camera.position.set(0, -maxDim * 1.2, maxDim * 1.0);
    camera.lookAt(0, 0, 0);
    controls.update();

    // Dynamically reposition standard lights high above the model [1.13.2]
    scene.traverse(function (object) {
        if (object.isDirectionalLight) {
            object.position.set(maxDim * 0.8, -maxDim * 0.8, maxDim * 2.0);
            object.intensity = 1.0;
        }
        if (object.isHemisphereLight) {
            object.position.set(0, 0, maxDim * 2.0);
            object.intensity = 0.5;
        }
    });
    // ─── BUILD BLOCK WALLS AND SOLE ──────────────────────────────────
    if (basePlane) scene.remove(basePlane);
    lateralWalls.forEach(wall => scene.remove(wall));
    lateralWalls = [];

    // Define a solid base depth (5% of max dimension) [1.13.2]
    const baseZ = -maxDim * 0.05;

    buildIrregularSole(geometry, baseZ);
    buildIrregularSideWalls(data, baseZ); 
    // ──────────────────────────────────────────────────────────────────

    console.log("[RockMorph 3D] Scene rendered and camera positioned.");
    document.getElementById("status-overlay").innerText = "Scene Status: " + data.element_id + " rendered.";
}



/**
 * Create an irregular horizontal sole (base) that mirrors the terrain's exact boundaries.
 * Highly optimized, uses the exact same geometry indices.
 */
function buildIrregularSole(terrainGeometry, baseZ) {
    const soleGeometry = terrainGeometry.clone();
    const positions = soleGeometry.attributes.position.array;

    // Flatten all vertices of the sole to the fixed bottom Z elevation [1.13.2]
    for (let i = 2; i < positions.length; i += 3) {
        positions[i] = baseZ;
    }
    soleGeometry.attributes.position.needsUpdate = true;
    soleGeometry.computeVertexNormals();

    const baseMaterial = new THREE.MeshStandardMaterial({
        color: 0x900000, // Professional solid brick red
        roughness: 0.8,
        metalness: 0.1,
        side: THREE.DoubleSide
    });

    basePlane = new THREE.Mesh(soleGeometry, baseMaterial);
    scene.add(basePlane);
}

/**
 * Generate precise vertical side walls along the irregular boundaries of the valid data.
 * Corrected with safe logical vertex indexing to prevent buffer offset corruption [1.13.2].
 */
function buildIrregularSideWalls(data, baseZ) {
    const width = data.width;
    const height = data.height;

    const isNoData = (r, c) => {
        const val = data.z_values[r][c];
        return val === null || val === undefined || isNaN(val) || val === data.nodata_value;
    };

    const validCells = [];
    for (let j = 0; j < height - 1; j++) {
        validCells[j] = new Uint8Array(width - 1);
        for (let i = 0; i < width - 1; i++) {
            validCells[j][i] = (!isNoData(j, i) && !isNoData(j, i + 1) && !isNoData(j + 1, i) && !isNoData(j + 1, i + 1)) ? 1 : 0;
        }
    }

    const wallPositions = [];
    const wallColors = [];
    wallVertexMappings = []; // Clear previous mappings

    const modelWidth = Math.abs(data.x_coords[data.x_coords.length - 1] - data.x_coords[0]);
    const modelHeight = Math.abs(data.y_coords[data.y_coords.length - 1] - data.y_coords[0]);

    for (let j = 0; j < height; j++) {
        for (let i = 0; i < width; i++) {
            // Rebuild indices on-the-fly, skipping NoData cells [1.13.2]
            if (j < height - 1 && i < width - 1) {
                const checkBorders = [
                    // Top horizontal edge
                    { rStart: j, cA: i, rB: j, cB: i + 1, isBoundary: (j === 0 && validCells[j][i]) || (j > 0 && validCells[j][i] !== validCells[j - 1][i]) },
                    // Bottom edge
                    { r: j + 1, c: i, isHoriz: true, cond: (j === height - 2) ? validCells[j][i] : (validCells[j][i] !== validCells[j + 1][i]) },
                    // Left edge
                    { r: j, c: i, isVert: true, cond: (i === 0 && validCells[j][i]) || (i > 0 && validCells[j][i] !== validCells[j][i - 1]) },
                    // Right edge
                    { r: j, c: i + 1, isVert: true, cond: (i === width - 2 ? validCells[j][i] : validCells[j][i] !== validCells[j][i + 1]) }
                ];

                // Let's use the clean single-pass edge extraction logic
            }
        }
    }

    // To keep it perfectly simple and avoid rewriting, let's just use the direct boundary loop:
    const indices = terrainGeometry = terrainMesh.geometry.index.array;
    const positions = terrainMesh.geometry.attributes.position.array;

    const edgeCount = {};
    const edgeVertices = {};

    for (let k = 0; k < indices.length; k += 3) {
        const v0 = indices[k];
        const v1 = indices[k + 1];
        const v2 = indices[k + 2];

        const tris_edges = [[v0, v1], [v1, v2], [v2, v0]];
        tris_edges.forEach(edge => {
            const key = Math.min(edge[0], edge[1]) + "_" + Math.max(edge[0], edge[1]);
            edgeCount[key] = (edgeCount[key] || 0) + 1;
            edgeVertices[key] = edge;
        });
    }

    for (const key in edgeCount) {
        if (edgeCount[key] === 1) {
            const edge = edgeVertices[key];
            const vA = edge[0];
            const vB = edge[1];

            const xa = positions[vA * 3];
            const ya = positions[vA * 3 + 1];
            const za = positions[vA * 3 + 2];

            const xb = positions[vB * 3];
            const yb = positions[vB * 3 + 1];
            const zb = positions[vB * 3 + 2];

            // Logical Vertex Index before we push these new 6 vertices [1.13.2]
            const startVertexIndex = wallPositions.length / 3;

            // Push 6 vertices to form a vertical quad
            wallPositions.push(xa, ya, za);     // TopA (vertex index: startVertexIndex + 0)
            wallPositions.push(xa, ya, baseZ);  // BottomA
            wallPositions.push(xb, yb, zb);     // TopB (vertex index: startVertexIndex + 2)

            wallPositions.push(xb, yb, zb);     // TopB dup (vertex index: startVertexIndex + 3)
            wallPositions.push(xa, ya, baseZ);  // BottomA dup
            wallPositions.push(xb, yb, baseZ);  // BottomB

            // Map ONLY the top vertices to their raw elevation data [1.13.2]
            // We read the raw elevations directly from the terrain positions buffer to remain 100% synchronized
            const rawZA = data.z_values[Math.round((0.5 - ya / modelHeight) * (height - 1))][Math.round((xa / modelWidth + 0.5) * (width - 1))];
            const rawZB = data.z_values[Math.round((0.5 - yb / modelHeight) * (height - 1))][Math.round((xb / modelWidth + 0.5) * (width - 1))];

            // Store logical vertex indices [1.13.2]
            wallVertexMappings.push({ rawZ: rawZA, vertexIndex: startVertexIndex + 0 }); // TopA
            wallVertexMappings.push({ rawZ: rawZB, vertexIndex: startVertexIndex + 2 }); // TopB
            wallVertexMappings.push({ rawZ: rawZB, vertexIndex: startVertexIndex + 3 }); // TopB dup

            for (let c = 0; c < 6; c++) {
                wallColors.push(0.0, 0.0, 0.8); // Blue debug
            }
        }
    }

    const wallGeometry = new THREE.BufferGeometry();
    wallGeometry.setAttribute('position', new THREE.Float32BufferAttribute(wallPositions, 3));
    wallGeometry.setAttribute('color', new THREE.Float32BufferAttribute(wallColors, 3));
    wallGeometry.computeVertexNormals();

    const wallMaterial = new THREE.MeshStandardMaterial({
        vertexColors: true,
        roughness: 0.6,
        metalness: 0.1,
        side: THREE.DoubleSide
    });

    const wallMesh = new THREE.Mesh(wallGeometry, wallMaterial);
    scene.add(wallMesh);
    lateralWalls.push(wallMesh);
}


/**
 * Handle dynamic vertical scaling for both the terrain mesh and the vertical walls.
 * Synchronized with safe logical vertex indexing.
 */
function updateZScale(scale) {
    if (!terrainMesh) return;
    currentZScale = scale;

    // 1. Update main terrain elevation vertices [1.13.2]
    const terrainGeo = terrainMesh.geometry;
    const terrainVertices = terrainGeo.attributes.position.array;
    let vertexIndex = 0;

    const height = originalDEMValues.length;
    const width = originalDEMValues[0].length;

    for (let j = 0; j < height; j++) {
        for (let i = 0; i < width; i++) {
            const rawZ = originalDEMValues[j][i];
            if (rawZ !== null && !isNaN(rawZ)) {
                terrainVertices[vertexIndex + 2] = (rawZ - spatialOffsets.z) * baseExaggeration * currentZScale;
            }
            vertexIndex += 3;
        }
    }
    terrainGeo.attributes.position.needsUpdate = true;
    terrainGeo.computeVertexNormals();

    // 2. Update ONLY the top vertices of the side walls in perfect sync [1.13.2]
    lateralWalls.forEach(wall => {
        const wallGeo = wall.geometry;
        const wallVertices = wallGeo.attributes.position.array;

        // Loop through logical vertex mappings to update the exact Z components [1.13.2]
        for (let k = 0; k < wallVertexMappings.length; k++) {
            const mapping = wallVertexMappings[k];
            const rawZ = mapping.rawZ;

            // Calculate the exact offset in the flat array: (Index * 3) + 2 [1.13.2]
            const zComponentIndex = (mapping.vertexIndex * 3) + 2;

            // Re-scale the elevation on the fly [1.13.2]
            wallVertices[zComponentIndex] = (rawZ - spatialOffsets.z) * baseExaggeration * currentZScale;
        }

        wallGeo.attributes.position.needsUpdate = true;
        wallGeo.computeVertexNormals();
    });
}


/**
 * Update shading style.
 */
function updateShadingMode(mode) {
    if (!terrainMesh) return;

    const material = terrainMesh.material;

    if (mode === "wireframe") {
        material.wireframe = true;
    } else {
        material.wireframe = false;
        material.flatShading = (mode === "flat");
        material.needsUpdate = true;
    }
}

function toggleWallsVisibility(visible) {
    console.log("[RockMorph 3D] Toggle walls visibility:", visible);
}

window.onload = initScene;