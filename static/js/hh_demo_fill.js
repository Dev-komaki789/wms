/*
 * hh_demo_fill.js — ハンディ入口のデモ用「番号タップ入力」。
 *
 * _hh_demo_codes.html が出すボタン（data-hh-fill="<コード>"）をタップすると、
 * その番号を入口の入力欄に流し込み、カメラスキャンと同じ経路
 * （input イベント → Enter keydown）で照会を実行する。
 * これにより、カメラが使えない iPhone でも番号を知らなくても操作できる。
 *
 * 対象入力欄のセレクタは camera_scan.js と同一（同じ経路に乗せるため）。
 */
(function () {
  'use strict';

  // camera_scan.js と同じ対象入力欄セレクタ（ハンディ筐体 .hh-screen 内のみ）。
  var INPUT_SELECTORS = [
    '.hh-screen input[data-hh-lookup]',
    '.hh-screen input.hh-key:not([inputmode="numeric"])',
  ];

  function findInput() {
    for (var i = 0; i < INPUT_SELECTORS.length; i++) {
      var el = document.querySelector(INPUT_SELECTORS[i]);
      if (el) return el;
    }
    return null;
  }

  function fill(text) {
    var input = findInput();
    if (!input) return;
    // camera_scan.js と同じ手順: 値を入れ、input → Enter を発火して既存ルートに乗せる。
    input.value = text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Enter',
      code: 'Enter',
      keyCode: 13,
      which: 13,
      bubbles: true,
      cancelable: true,
    }));
  }

  // ボタンは include テンプレートで動的に描画されるため、document 委譲で拾う。
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-hh-fill]');
    if (!btn) return;
    e.preventDefault();
    fill(btn.getAttribute('data-hh-fill'));
  });
})();
