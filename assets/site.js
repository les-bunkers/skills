/* The scroll position drives a playhead across the twelve stages.
   Without this file the page reads as a plain column: each stage keeps its own figure. */
(function () {
  'use strict';

  var root = document.documentElement;

  try {
    run();
  } catch (err) {
    root.classList.remove('js-reel', 'js-intro');
    if (window.console) console.error(err);
  }

  function run() {
    const $ = (sel, el) => (el || document).querySelector(sel);
    const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));
    const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
    const pad = (n) => String(n).padStart(2, '0');

    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
    const wide = window.matchMedia('(min-width: 1181px)');

    const screen = $('#ecran');
    const tl = $('#timeline');
    const sections = [$('.hero')].concat($$('.stage'));
    const figs = sections.map((s) => $('.fig', s));
    const slots = sections.map((s) => $('.hero__fig, .stage__fig', s));
    const clips = $$('.clip', tl);
    const checks = $$('.cell[data-ci]', tl);
    const son = $('#tl-son');
    const texte = $('#tl-texte');
    const out = {
      source: $('#ecran-source'),
      stage: $('#ecran-etape'),
      code: $('#tl-code'),
      n: $('#tl-n'),
      name: $('#tl-name'),
    };
    const video = $('video', figs[0]);
    const last = sections.length - 1;

    let active = -1;
    let allDone = null;
    let frame = 0;
    let ripTimer = 0;

    /* ── listening: one sound at a time ── */
    const player = { audio: null, btn: null, raf: 0 };
    const sounds = {};

    function stop() {
      window.cancelAnimationFrame(player.raf);
      if (player.audio) player.audio.pause();
      if (player.btn) {
        player.btn.setAttribute('aria-pressed', 'false');
        player.btn.style.removeProperty('--t');
      }
      player.btn = null;
    }

    function tick() {
      const a = player.audio;
      if (!player.btn || !a) return;
      if (a.duration) player.btn.style.setProperty('--t', (a.currentTime / a.duration).toFixed(3));
      player.raf = window.requestAnimationFrame(tick);
    }

    $$('.ab__btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const fig = btn.closest('.fig');
        if (btn.dataset.view && fig) {
          window.clearTimeout(ripTimer);
          fig.dataset.view = btn.dataset.view;
        }
        if (player.btn === btn) {
          stop();
          return;
        }
        stop();
        const src = btn.dataset.src;
        const audio = sounds[src] || (sounds[src] = new Audio(src));
        audio.currentTime = 0;
        audio.onended = stop;
        player.audio = audio;
        player.btn = btn;
        btn.setAttribute('aria-pressed', 'true');
        const started = audio.play();
        if (started && started.catch) started.catch(stop);
        tick();
      });
    });

    /* ── a figure turns on when its stage is the one being read ── */
    function setOn(fig, on) {
      if (fig.classList.contains('is-on') === on) return;
      fig.classList.toggle('is-on', on);
      if (fig.classList.contains('fig--rip')) {
        window.clearTimeout(ripTimer);
        if (!on) fig.dataset.view = 'avant';
        else if (reduce.matches) fig.dataset.view = 'apres';
        else {
          ripTimer = window.setTimeout(() => {
            fig.dataset.view = 'apres';
          }, 1500);
        }
      }
      if (!on && player.btn && fig.contains(player.btn)) stop();
    }

    function syncVideo() {
      if (!video) return;
      const r = figs[0].getBoundingClientRect();
      const shown = figs[0].classList.contains('is-on') && r.bottom > 0 && r.top < window.innerHeight;
      if (shown && !reduce.matches) {
        if (video.paused) {
          const started = video.play();
          if (started && started.catch) started.catch(() => {});
        }
      } else if (!video.paused) {
        video.pause();
      }
    }

    function paint(i, done) {
      clips.forEach((clip, k) => {
        const n = k + 1;
        const state = done || n < i ? 'done' : n === i ? 'doing' : 'todo';
        clip.dataset.state = state;
        if (state === 'doing') clip.setAttribute('aria-current', 'step');
        else clip.removeAttribute('aria-current');
      });
      checks.forEach((cell) => {
        const n = Number(cell.dataset.ci);
        if (done || n < i) cell.dataset.state = 'done';
        else if (n === i) cell.dataset.state = 'doing';
        else delete cell.dataset.state;
      });
      son.toggleAttribute('data-on', i >= 2);
      son.dataset.mode = i >= 6 ? 'master' : 'brut';
      son.toggleAttribute('data-heard', i >= 3);
      son.toggleAttribute('data-tight', i >= 5);
      texte.toggleAttribute('data-on', i >= 3);
      texte.toggleAttribute('data-tight', i >= 5);

      const name = sections[i].dataset.name;
      out.source.textContent = figs[i].dataset.source;
      out.stage.textContent = i ? pad(i) + ' / 12 · ' + name : name;
      out.code.textContent = pad(i) + ' / 12';
      out.n.textContent = i ? 'Étape ' + pad(i) + ' / 12' : 'Départ';
      out.name.textContent = name;
    }

    function update() {
      frame = 0;
      const vh = window.innerHeight;
      const navH = parseFloat(window.getComputedStyle(root).getPropertyValue('--nav-h')) || 76;
      const focus = navH + (vh - navH - tl.offsetHeight) * 0.45;

      let i = 0;
      let rect = null;
      for (let k = 0; k <= last; k += 1) {
        const r = sections[k].getBoundingClientRect();
        if (r.top > focus) break;
        i = k;
        rect = r;
      }
      const p = i > 0 ? i - 1 + clamp((focus - rect.top) / rect.height, 0, 1) : 0;
      const done = p >= 12;
      tl.style.setProperty('--p', p.toFixed(3));

      if (i !== active || done !== allDone) {
        active = i;
        allDone = done;
        paint(i, done);
      }

      if (wide.matches) {
        figs.forEach((fig, k) => setOn(fig, k === i));
      } else {
        figs.forEach((fig) => {
          if (fig.classList.contains('is-on')) return;
          const r = fig.getBoundingClientRect();
          if (r.top < vh * 0.86 && r.bottom > navH) setOn(fig, true);
        });
      }
      syncVideo();
    }

    function ask() {
      if (!frame) frame = window.requestAnimationFrame(update);
    }

    /* wide screens: every figure plays in the one monitor; otherwise each sits under its stage */
    function place() {
      const inMonitor = wide.matches;
      figs.forEach((fig, k) => {
        const home = inMonitor ? screen : slots[k];
        if (fig.parentNode !== home) home.appendChild(fig);
        if (inMonitor) setOn(fig, false);
      });
      active = -1;
      update();
    }

    /* ── copy a command ── */
    const copied = $('#copie-etat');

    function copyByHand(text) {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try {
        ok = document.execCommand('copy');
      } catch (e) {
        ok = false;
      }
      document.body.removeChild(ta);
      return ok;
    }

    $$('.copy').forEach((btn) => {
      let timer = 0;
      const confirm = () => {
        btn.setAttribute('data-done', '');
        if (copied) copied.textContent = 'Commande copiée: ' + btn.dataset.copy;
        window.clearTimeout(timer);
        timer = window.setTimeout(() => {
          btn.removeAttribute('data-done');
          if (copied) copied.textContent = '';
        }, 1600);
      };
      btn.addEventListener('click', () => {
        const text = btn.dataset.copy;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(confirm, () => {
            if (copyByHand(text)) confirm();
          });
        } else if (copyByHand(text)) {
          confirm();
        }
      });
    });

    /* ── the opening: one typed line, then the timeline unfolds ── */
    const typed = $('#frappe');
    if (typed && !reduce.matches) {
      const full = typed.textContent;
      let k = 0;
      typed.textContent = '';
      const type = () => {
        k += 1;
        typed.textContent = full.slice(0, k);
        if (k < full.length) window.setTimeout(type, 70);
      };
      window.setTimeout(type, 380);
    }
    window.setTimeout(() => root.classList.remove('js-intro'), 2800);

    window.addEventListener('scroll', ask, { passive: true });
    window.addEventListener('resize', ask);
    window.addEventListener('load', ask);
    wide.addEventListener('change', place);
    reduce.addEventListener('change', ask);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(ask);

    place();
  }
})();
