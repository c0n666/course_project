/* Nutrition & Workout — shared UI behaviour (toasts, chart defaults). */
(function () {
  'use strict';

  /* ---------- Chart.js defaults (runs before page scripts) ---------- */
  if (window.Chart) {
    Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
    Chart.defaults.font.size = 12;
    Chart.defaults.color = '#64748B';
    Chart.defaults.borderColor = '#F1F5F9';
    Chart.defaults.plugins.legend.labels.usePointStyle = true;
    Chart.defaults.plugins.legend.labels.boxWidth = 8;
    Chart.defaults.plugins.tooltip.backgroundColor = '#0F172A';
    Chart.defaults.plugins.tooltip.padding = 10;
    Chart.defaults.plugins.tooltip.cornerRadius = 10;
  }

  /* ---------- Toasts ---------- */
  const DURATION = { success: 4500, info: 5000, error: 8000 };

  function dismiss(toast) {
    if (!toast || toast.classList.contains('is-leaving')) return;
    toast.classList.add('is-leaving');
    toast.addEventListener('animationend', () => toast.remove(), { once: true });
    // Fallback when animations are disabled
    setTimeout(() => toast.remove(), 300);
  }

  function setup(toast) {
    const kind = toast.dataset.kind || 'info';
    const total = DURATION[kind] || DURATION.info;
    const bar = toast.querySelector('.toast-progress');
    let remaining = total;
    let started = Date.now();
    let timer = null;

    function start() {
      started = Date.now();
      timer = setTimeout(() => dismiss(toast), remaining);
      if (bar) bar.style.animationPlayState = 'running';
    }
    function pause() {
      clearTimeout(timer);
      remaining -= Date.now() - started;
      if (bar) bar.style.animationPlayState = 'paused';
    }

    if (bar) bar.style.animation = `toast-progress ${total}ms linear forwards`;

    toast.querySelector('[data-toast-close]')?.addEventListener('click', () => dismiss(toast));
    toast.addEventListener('mouseenter', pause);
    toast.addEventListener('mouseleave', start);
    toast.addEventListener('focusin', pause);
    toast.addEventListener('focusout', start);
    start();
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.toast').forEach(setup);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const last = [...document.querySelectorAll('.toast:not(.is-leaving)')].pop();
    // Do not steal Escape from an open modal
    if (last && !document.querySelector('.fixed.inset-0:not(.hidden)')) dismiss(last);
  });
})();
