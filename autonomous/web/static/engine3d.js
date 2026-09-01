/**
 * A working V8, built procedurally in WebGL.
 *
 * Modelled on the Koenigsegg twin-turbo V8: 90-degree banks, gold intake
 * manifold in the valley, turbos outboard, carbon runners. The internals are
 * real geometry driven by slider-crank motion, not a canned animation - the
 * cutaway toggle shows the pistons and rods actually working.
 *
 * Each cylinder is a branch of interest. Hovering lifts its runner, clicking
 * selects it. Cylinders beyond your branch count sit dark and idle.
 */

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const BANK_ANGLE = Math.PI / 4; // 45 degrees each side => a 90-degree V
const CYL_PER_BANK = 4;
const CYL_SPACING = 1.35;
const CRANK_RADIUS = 0.42;
const ROD_LENGTH = 1.5;
const BORE = 0.52;

// Firing order of a cross-plane V8, as crank-pin angles.
const PIN_ANGLE = [0, Math.PI / 2, Math.PI, (3 * Math.PI) / 2];

const COLORS = {
  metal: 0x33373c,
  darkMetal: 0x1e2124,
  carbon: 0x141618,
  gold: 0xc9922f,
  goldBright: 0xe6b552,
  accent: 0x4f9bf0,
};

function material(color, metalness, roughness, extra = {}) {
  return new THREE.MeshStandardMaterial({ color, metalness, roughness, ...extra });
}

/** A vertical gradient environment, so the metal has something to reflect. */
function buildEnvironment(renderer) {
  const canvas = document.createElement("canvas");
  canvas.width = 16;
  canvas.height = 128;
  const ctx = canvas.getContext("2d");
  const gradient = ctx.createLinearGradient(0, 0, 0, 128);
  gradient.addColorStop(0.0, "#9aa3ad");
  gradient.addColorStop(0.35, "#4a5158");
  gradient.addColorStop(0.62, "#1b1e21");
  gradient.addColorStop(1.0, "#0a0b0c");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 16, 128);

  const texture = new THREE.CanvasTexture(canvas);
  texture.mapping = THREE.EquirectangularReflectionMapping;
  texture.colorSpace = THREE.SRGBColorSpace;

  const pmrem = new THREE.PMREMGenerator(renderer);
  const environment = pmrem.fromEquirectangular(texture).texture;
  pmrem.dispose();
  texture.dispose();
  return environment;
}

/** The gold intake plenum: a swept wing sitting in the valley. */
function buildIntakeWing(materials) {
  const shape = new THREE.Shape();
  shape.moveTo(0, 0.42);
  shape.bezierCurveTo(1.1, 0.5, 2.3, 0.28, 3.0, -0.22);
  shape.lineTo(3.0, -0.52);
  shape.bezierCurveTo(2.2, -0.16, 1.0, 0.02, 0, -0.06);
  shape.closePath();

  const geometry = new THREE.ExtrudeGeometry(shape, {
    depth: 0.5,
    bevelEnabled: true,
    bevelSize: 0.09,
    bevelThickness: 0.09,
    bevelSegments: 4,
    curveSegments: 24,
  });
  geometry.center();

  const wing = new THREE.Mesh(geometry, materials.gold);
  wing.castShadow = true;
  return wing;
}

function buildTurbo(materials) {
  const turbo = new THREE.Group();

  const snail = new THREE.Mesh(new THREE.TorusGeometry(0.46, 0.26, 16, 28), materials.darkMetal);
  snail.rotation.y = Math.PI / 2;
  turbo.add(snail);

  const housing = new THREE.Mesh(
    new THREE.CylinderGeometry(0.34, 0.4, 0.55, 24),
    materials.metal
  );
  housing.rotation.z = Math.PI / 2;
  turbo.add(housing);

  // The compressor wheel, which spins.
  const wheel = new THREE.Group();
  for (let i = 0; i < 9; i += 1) {
    const blade = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.3, 0.13), materials.bright);
    blade.position.set(0, 0, 0);
    blade.rotation.x = (i / 9) * Math.PI * 2;
    blade.translateY(0.17);
    blade.rotation.z = 0.5;
    wheel.add(blade);
  }
  wheel.rotation.z = Math.PI / 2;
  wheel.position.x = 0.3;
  turbo.add(wheel);
  turbo.userData.wheel = wheel;

  const inlet = new THREE.Mesh(new THREE.CylinderGeometry(0.3, 0.3, 0.22, 20), materials.carbon);
  inlet.rotation.z = Math.PI / 2;
  inlet.position.x = 0.42;
  turbo.add(inlet);

  return turbo;
}

/** Bore axis for a bank: left is -Z, right is +Z, both leaning up. */
function bankAxis(side) {
  return new THREE.Vector3(0, Math.cos(BANK_ANGLE), side * Math.sin(BANK_ANGLE)).normalize();
}

export function createEngine({ container, onSelect, onHover }) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.5;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.appendChild(renderer.domElement);
  renderer.domElement.style.display = "block";
  renderer.domElement.style.touchAction = "none";

  const scene = new THREE.Scene();
  scene.environment = buildEnvironment(renderer);

  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100);
  camera.position.set(6.0, 3.4, 7.0);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.07;
  controls.minDistance = 5;
  controls.maxDistance = 18;
  controls.maxPolarAngle = Math.PI * 0.85;
  controls.target.set(0, 0.6, 0);

  // --- lighting: a dark studio, keyed from above with rims on both flanks ---
  scene.add(new THREE.AmbientLight(0xffffff, 0.5));
  const key = new THREE.DirectionalLight(0xffffff, 3.4);
  key.position.set(5, 10, 6);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.camera.near = 1;
  key.shadow.camera.far = 30;
  key.shadow.bias = -0.0008;
  scene.add(key);

  const rimLeft = new THREE.SpotLight(0xbcd4ff, 90, 25, Math.PI / 5, 0.6, 2);
  rimLeft.position.set(-8, 3, -6);
  scene.add(rimLeft);
  const rimRight = new THREE.SpotLight(0xffd9a8, 90, 25, Math.PI / 5, 0.6, 2);
  rimRight.position.set(8, 2, -5);
  scene.add(rimRight);
  // A low fill so the sump and lower block do not go to pure black.
  const fill = new THREE.DirectionalLight(0xaebdd0, 0.85);
  fill.position.set(-4, 1.5, 7);
  scene.add(fill);

  const materials = {
    metal: material(COLORS.metal, 0.95, 0.32),
    darkMetal: material(COLORS.darkMetal, 0.9, 0.42),
    carbon: material(COLORS.carbon, 0.45, 0.55),
    gold: material(COLORS.gold, 1.0, 0.26),
    bright: material(0x9fb0c0, 1.0, 0.18),
    idle: material(0x2a2d30, 0.8, 0.6),
  };
  materials.goldGlow = material(COLORS.goldBright, 1.0, 0.24, {
    emissive: new THREE.Color(COLORS.accent),
    emissiveIntensity: 0.3,
  });
  // Cutaway: the bank castings turn to glass so the internals are visible.
  materials.cutawayGhost = new THREE.MeshPhysicalMaterial({
    color: COLORS.metal,
    metalness: 0.6,
    roughness: 0.35,
    transparent: true,
    opacity: 0.22,
    side: THREE.DoubleSide,
    depthWrite: false,
  });

  const engine = new THREE.Group();
  scene.add(engine);

  // --- crankcase and sump ------------------------------------------------
  const spanX = CYL_PER_BANK * CYL_SPACING;
  const block = new THREE.Mesh(new THREE.BoxGeometry(spanX + 0.5, 1.5, 2.3), materials.metal);
  block.position.y = -0.55;
  block.castShadow = true;
  block.receiveShadow = true;
  engine.add(block);

  const sump = new THREE.Mesh(
    new THREE.BoxGeometry(spanX + 0.1, 0.75, 1.85),
    materials.darkMetal
  );
  sump.position.y = -1.55;
  sump.castShadow = true;
  engine.add(sump);

  // --- crankshaft --------------------------------------------------------
  const crank = new THREE.Group();
  const crankShaft = new THREE.Mesh(
    new THREE.CylinderGeometry(0.16, 0.16, spanX + 0.7, 20),
    materials.bright
  );
  crankShaft.rotation.z = Math.PI / 2;
  crank.add(crankShaft);
  engine.add(crank);

  const cutawayParts = [];
  const internals = [];
  const cylinders = [];

  // --- banks -------------------------------------------------------------
  for (const side of [-1, 1]) {
    const axis = bankAxis(side);
    const bank = new THREE.Group();
    engine.add(bank);

    // The bank casting the bores are cut into.
    const bankBody = new THREE.Mesh(
      new THREE.BoxGeometry(spanX + 0.35, 1.9, 1.15),
      materials.metal
    );
    const bankCentre = axis.clone().multiplyScalar(1.35);
    bankBody.position.copy(bankCentre);
    bankBody.rotation.x = -side * BANK_ANGLE;
    bankBody.castShadow = true;
    bank.add(bankBody);
    cutawayParts.push(bankBody);

    // Cam cover along the top of the bank.
    const cover = new THREE.Mesh(
      new THREE.BoxGeometry(spanX + 0.2, 0.42, 0.95),
      materials.carbon
    );
    cover.position.copy(axis.clone().multiplyScalar(2.42));
    cover.rotation.x = -side * BANK_ANGLE;
    cover.castShadow = true;
    bank.add(cover);

    for (let i = 0; i < CYL_PER_BANK; i += 1) {
      const x = (i - (CYL_PER_BANK - 1) / 2) * CYL_SPACING;
      const index = side < 0 ? i : i + CYL_PER_BANK;

      const group = new THREE.Group();
      group.position.x = x;
      bank.add(group);

      // Intake runner: the part that lifts when you hover it.
      const runner = new THREE.Group();
      const runnerTube = new THREE.Mesh(
        new THREE.CylinderGeometry(0.21, 0.25, 0.85, 18),
        materials.carbon
      );
      runnerTube.castShadow = true;
      runner.add(runnerTube);
      const trumpet = new THREE.Mesh(
        new THREE.CylinderGeometry(0.3, 0.2, 0.22, 18, 1, true),
        materials.gold
      );
      trumpet.position.y = 0.5;
      runner.add(trumpet);

      const runnerBase = axis.clone().multiplyScalar(2.95);
      // Runners lean inward, toward the plenum in the valley.
      runnerBase.z -= side * 0.35;
      runner.position.copy(runnerBase);
      runner.rotation.x = -side * (BANK_ANGLE * 0.55);
      group.add(runner);

      // Exhaust stub, outboard.
      const exhaust = new THREE.Mesh(
        new THREE.TorusGeometry(0.28, 0.09, 10, 20, Math.PI * 0.75),
        materials.bright
      );
      exhaust.position.copy(axis.clone().multiplyScalar(1.6));
      exhaust.position.z += side * 0.72;
      exhaust.rotation.set(Math.PI / 2, 0, side * 1.2);
      group.add(exhaust);

      // --- internals: bore, piston, rod --------------------------------
      const boreTube = new THREE.Mesh(
        new THREE.CylinderGeometry(BORE, BORE, 1.7, 22, 1, true),
        new THREE.MeshStandardMaterial({
          color: 0x0d0f11,
          metalness: 0.7,
          roughness: 0.5,
          side: THREE.BackSide,
        })
      );
      boreTube.position.copy(axis.clone().multiplyScalar(1.4));
      boreTube.rotation.x = -side * BANK_ANGLE;
      boreTube.position.x = x;
      boreTube.visible = false;
      engine.add(boreTube);

      const piston = new THREE.Mesh(
        new THREE.CylinderGeometry(BORE - 0.04, BORE - 0.04, 0.46, 22),
        materials.bright
      );
      piston.rotation.x = -side * BANK_ANGLE;
      piston.visible = false;
      engine.add(piston);

      const rod = new THREE.Mesh(
        new THREE.BoxGeometry(0.14, ROD_LENGTH, 0.2),
        materials.darkMetal
      );
      rod.visible = false;
      engine.add(rod);

      internals.push({ piston, rod, boreTube, axis, x, side, pin: PIN_ANGLE[i] });

      cylinders.push({
        index,
        group,
        runner,
        runnerBase: runnerBase.clone(),
        meshes: [runnerTube, trumpet],
        trumpet,
        active: false,
      });
    }
  }

  // --- gold intake plenum in the valley ---------------------------------
  // The signature gold plenum, seated down between the banks.
  // A swept wing each side of a central throttle body, running the length of
  // the engine rather than across it.
  const wingScale = new THREE.Vector3((spanX + 0.4) / 3.0, 0.8, 1.5);
  for (const side of [-1, 1]) {
    const wing = buildIntakeWing(materials);
    wing.scale.copy(wingScale);
    wing.scale.z *= side;
    wing.position.set(0, 2.6, side * 0.42);
    engine.add(wing);
  }

  const throttleBody = new THREE.Mesh(
    new THREE.CylinderGeometry(0.34, 0.4, 0.5, 24),
    materials.darkMetal
  );
  throttleBody.position.set(0, 3.05, 0);
  engine.add(throttleBody);

  // --- turbos, outboard on each flank -----------------------------------
  const turbos = [];
  for (const side of [-1, 1]) {
    const turbo = buildTurbo(materials);
    turbo.position.set(side * 1.5, 0.15, side * 2.05);
    turbo.rotation.y = side * 0.35;
    engine.add(turbo);
    turbos.push(turbo);
  }

  // A dark ground plane, only to catch the shadow.
  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(40, 40),
    new THREE.ShadowMaterial({ opacity: 0.42 })
  );
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -2.05;
  ground.receiveShadow = true;
  scene.add(ground);

  // --- interaction -------------------------------------------------------
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let hovered = null;
  let selected = null;
  let cutaway = false;
  let branches = [];

  function pickable() {
    return cylinders.flatMap((c) => c.meshes);
  }

  function cylinderFor(object) {
    return cylinders.find((c) => c.meshes.includes(object)) || null;
  }

  function updatePointer(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  }

  function pick() {
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects(pickable(), false);
    return hits.length ? cylinderFor(hits[0].object) : null;
  }

  renderer.domElement.addEventListener("pointermove", (event) => {
    updatePointer(event);
    const found = pick();
    const slug = found && branches[found.index] ? branches[found.index].slug : null;
    if (found !== hovered) {
      hovered = found;
      renderer.domElement.style.cursor = slug ? "pointer" : "grab";
      if (onHover) onHover(slug);
    }
  });

  renderer.domElement.addEventListener("pointerleave", () => {
    hovered = null;
    if (onHover) onHover(null);
  });

  renderer.domElement.addEventListener("pointerdown", (event) => {
    updatePointer(event);
    const found = pick();
    if (found && branches[found.index] && onSelect) {
      onSelect(branches[found.index].slug);
    }
  });

  // --- animation ---------------------------------------------------------
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const clock = new THREE.Clock();
  let angle = 0;
  let rpm = 1.9;
  let targetRpm = 1.9;
  let running = true;
  let frames = 0;

  // Preallocated: the frame loop must not allocate.
  const up = new THREE.Vector3(0, 1, 0);
  const tmp = new THREE.Vector3();
  const rodDir = new THREE.Vector3();
  const liftTarget = new THREE.Vector3();
  const AXIS_LEFT = bankAxis(-1);
  const AXIS_RIGHT = bankAxis(1);

  function positionInternals() {
    for (const part of internals) {
      const phi = angle + part.pin - part.side * BANK_ANGLE;
      // Slider-crank: piston distance from the crank centre along the bore.
      const sin = Math.sin(phi);
      const displacement =
        CRANK_RADIUS * Math.cos(phi) +
        Math.sqrt(Math.max(ROD_LENGTH ** 2 - (CRANK_RADIUS * sin) ** 2, 0.0001));

      tmp.copy(part.axis).multiplyScalar(displacement);
      part.piston.position.set(part.x, tmp.y, tmp.z);

      // The crank pin, and the rod that reaches it.
      const pinY = CRANK_RADIUS * Math.cos(angle + part.pin);
      const pinZ = CRANK_RADIUS * Math.sin(angle + part.pin);
      part.rod.position.set(
        part.x,
        (part.piston.position.y + pinY) / 2,
        (part.piston.position.z + pinZ) / 2
      );
      rodDir.set(0, part.piston.position.y - pinY, part.piston.position.z - pinZ);
      part.rod.scale.y = rodDir.length() / ROD_LENGTH;
      part.rod.quaternion.setFromUnitVectors(up, rodDir.normalize());
    }
  }

  function frame() {
    if (!running) return;
    const delta = Math.min(clock.getDelta(), 0.05);

    rpm += (targetRpm - rpm) * Math.min(delta * 2.5, 1);
    if (!reduceMotion) {
      angle += delta * rpm * Math.PI * 2;
      crank.rotation.x = angle;
      for (const turbo of turbos) turbo.userData.wheel.rotation.z += delta * rpm * 14;
      // A little idle shake, scaled with revs.
      engine.position.y = Math.sin(angle * 4) * 0.006 * rpm;
      engine.rotation.z = Math.sin(angle * 2.3) * 0.0016 * rpm;
    }

    if (cutaway) positionInternals();

    // Hover and selection lift the runner and warm the trumpet.
    for (const cylinder of cylinders) {
      const isHovered = cylinder === hovered && cylinder.active;
      const isSelected = cylinder.active && selected === cylinder.index;
      const target = isSelected ? 0.4 : isHovered ? 0.28 : 0;
      liftTarget
        .copy(cylinder.runnerBase)
        .addScaledVector(cylinder.index < CYL_PER_BANK ? AXIS_LEFT : AXIS_RIGHT, target);
      cylinder.runner.position.lerp(liftTarget, Math.min(delta * 9, 1));
      const glow = isSelected ? 0.55 : isHovered ? 0.3 : 0;
      cylinder.trumpet.material = glow > 0 ? materials.goldGlow : materials.gold;
      if (glow > 0) materials.goldGlow.emissiveIntensity = glow;
    }

    controls.update();
    renderer.render(scene, camera);
    frames += 1;
    requestAnimationFrame(frame);
  }

  function resize() {
    const width = container.clientWidth || 640;
    const height = container.clientHeight || 380;
    // updateStyle must stay on: with it off the canvas lays out at its
    // device-pixel size and overflows the container.
    renderer.setSize(width, height);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }

  const observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();
  requestAnimationFrame(frame);

  return {
    /** Map branches onto cylinders; the rest go dark. */
    setBranches(list) {
      branches = list;
      for (const cylinder of cylinders) {
        const branch = list[cylinder.index];
        cylinder.active = Boolean(branch);
        const hasBrief = Boolean(branch && branch.brief);
        for (const mesh of cylinder.meshes) {
          mesh.material =
            mesh === cylinder.trumpet
              ? hasBrief
                ? materials.gold
                : materials.idle
              : cylinder.active
                ? materials.carbon
                : materials.idle;
        }
      }
    },
    setSelected(slug) {
      const found = branches.findIndex((b) => b && b.slug === slug);
      selected = found >= 0 ? found : null;
    },
    setCutaway(on) {
      cutaway = on;
      for (const part of cutawayParts) {
        part.material = on ? materials.cutawayGhost : materials.metal;
      }
      for (const part of internals) {
        part.piston.visible = on;
        part.rod.visible = on;
        part.boreTube.visible = on;
      }
      if (on) positionInternals();
    },
    setRevs(value) {
      targetRpm = value;
    },
    /** Debug handles: used by the test harness to prove the motion is real. */
    get frameCount() {
      return frames;
    },
    get crankAngle() {
      return angle;
    },
    pistonHeights() {
      return internals.map((part) => Number(part.piston.position.y.toFixed(4)));
    },
    dispose() {
      running = false;
      observer.disconnect();
      controls.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
