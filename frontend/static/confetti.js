(function (global) {
  "use strict";

  var canvas = null;
  var context = null;
  var particles = [];
  var animationFrame = null;

  function resizeCanvas() {
    if (!canvas || !context) return;

    var pixelRatio = window.devicePixelRatio || 1;
    canvas.width = window.innerWidth * pixelRatio;
    canvas.height = window.innerHeight * pixelRatio;
    canvas.style.width = window.innerWidth + "px";
    canvas.style.height = window.innerHeight + "px";
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  }

  function getCanvas() {
    if (canvas) return canvas;

    canvas = document.getElementById("confetti-canvas");
    if (!canvas) {
      canvas = document.createElement("canvas");
      canvas.id = "confetti-canvas";
      canvas.style.position = "fixed";
      canvas.style.zIndex = "1001";
      canvas.style.top = "0";
      canvas.style.left = "0";
      canvas.style.pointerEvents = "none";
      document.body.appendChild(canvas);
    }

    context = canvas.getContext("2d");
    if (!context) {
      canvas = null;
      throw new Error("Could not initialize the victory confetti canvas.");
    }
    resizeCanvas();
    return canvas;
  }

  function renderFrame() {
    animationFrame = null;
    context.clearRect(0, 0, window.innerWidth, window.innerHeight);
    particles = particles.filter(function (particle) {
      particle.x += particle.vx;
      particle.y += particle.vy;
      particle.vy += particle.gravity;
      particle.vx *= particle.decay;
      particle.vy *= particle.decay;
      particle.opacity = 1 - particle.tick / particle.ticks;
      particle.tick += 1;

      context.save();
      context.globalAlpha = particle.opacity;
      context.fillStyle = particle.color;
      context.fillRect(particle.x, particle.y, particle.width, particle.height);
      context.restore();

      return particle.tick < particle.ticks
        && particle.y < window.innerHeight
        && particle.x > -particle.width
        && particle.x < window.innerWidth + particle.width;
    });

    if (particles.length > 0) {
      animationFrame = window.requestAnimationFrame(renderFrame);
    }
  }

  function confetti(options) {
    var settings = options || {};
    getCanvas();

    var count = settings.particleCount || 50;
    var angle = (settings.angle === undefined ? 90 : settings.angle) * Math.PI / 180;
    var spread = (settings.spread === undefined ? 45 : settings.spread) * Math.PI / 180;
    var startVelocity = settings.startVelocity || 55;
    var decay = settings.decay || 0.9;
    var colors = settings.colors || [
      "#26ccff",
      "#a25afd",
      "#ff5e7e",
      "#88ff5a",
      "#fcff42",
      "#ffd066",
      "#1febff",
    ];
    var ticks = settings.ticks || 200;
    var origin = settings.origin || { x: 0.5, y: 0.5 };
    var gravity = settings.gravity === undefined ? 0.9 : settings.gravity;
    var drift = settings.drift || 0;

    for (var index = 0; index < count; index += 1) {
      var particleAngle = angle - spread / 2 + Math.random() * spread;
      var velocity = startVelocity + (Math.random() - 0.5) * startVelocity * 0.5;
      particles.push({
        x: window.innerWidth * origin.x,
        y: window.innerHeight * origin.y,
        width: Math.random() * 8 + 4,
        height: Math.random() * 8 + 4,
        vx: velocity * Math.cos(particleAngle) + drift * (Math.random() - 0.5),
        vy: -velocity * Math.sin(particleAngle),
        color: colors[index % colors.length],
        opacity: 1,
        ticks: ticks,
        tick: 0,
        decay: decay,
        gravity: gravity * 0.5,
      });
    }

    if (animationFrame === null) {
      animationFrame = window.requestAnimationFrame(renderFrame);
    }
  }

  window.addEventListener("resize", resizeCanvas);
  global.confetti = confetti;
}(window));
