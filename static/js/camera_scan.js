/**
 * ハンディ画面のバーコード入力欄にカメラスキャン機能を自動で組み込む。
 *
 * 既存のスキャナー入力欄（業務用ハンディスキャナの「テキスト + Enter」入力に
 * 寄せた UI）に対し、横にカメラボタンを追加する。ボタンを押すと全画面の
 * カメラプレビュー（モーダル）が開き、ブラウザ標準の BarcodeDetector で
 * バーコード（Code 128。アプリが印刷する形式に限定）を読み取って入力欄に値を
 * 流し込み、Enter キーをディスパッチして既存のスキャン処理ルートに乗せる。
 *
 * 対象セレクタ（ハンディ筐体 `.hh-screen` 内のみ）:
 *   - `input[data-hh-lookup]`         （計画外入出庫・棚間移動・棚卸など）
 *   - `input.hh-key[inputmode="text"]`（受入/検品/棚入れ/ピッキング/出荷検品など）
 *
 * BarcodeDetector が無いブラウザでは何もしない（ボタン自体出さない）。
 * Android Chrome / Edge は対応済み、iOS Safari は将来 ZXing-js でフォールバック
 * 予定だが、当面は実機 Pixel での動作確認用途で十分。
 *
 * セキュアコンテキスト要件: ブラウザの `getUserMedia` は HTTPS または
 * `localhost` でのみ動く。開発時は ADB Reverse 経由で
 * `http://localhost:8000/` で見ているので動作する。
 */
(function () {
  'use strict';

  // BarcodeDetector が無い環境では何もしない（カメラボタンも出さない）
  const hasDetector = ('BarcodeDetector' in window);

  // 対象セレクタ: ハンディ画面 (.hh-screen) 配下のバーコード入力欄のみ。
  // 数量欄 (inputmode="numeric") は除外。
  // - 計画外入庫/出庫/棚間移動/棚卸: input[data-hh-lookup]
  // - 入荷受入/検品/棚入れ/ピッキング/出荷検品など: input.hh-key
  //   （inputmode 未指定の指示番号入力欄も拾うため `[inputmode="text"]` で
  //    絞らない。代わりに `inputmode="numeric"` を否定する）
  const TARGET_SELECTOR = [
    '.hh-screen input[data-hh-lookup]',
    '.hh-screen input.hh-key:not([inputmode="numeric"])',
  ].join(',');

  function injectAll() {
    if (!hasDetector) return;
    document.querySelectorAll(TARGET_SELECTOR).forEach(injectButton);
  }

  function injectButton(input) {
    if (input.dataset.hhCamInjected) return;
    input.dataset.hhCamInjected = '1';

    // input-group が無ければ作って input をラップする。
    // 既存 input-group の場合は input の直後にカメラボタンを差し込む
    // （照合ボタンなど別の append-button がある場合はその左側に置く）。
    let group = input.closest('.input-group');
    if (!group) {
      group = document.createElement('div');
      group.className = 'input-group';
      // input に付いている mb-* / mt-* / m-* など余白クラスはラッパに移す。
      // そうしないと input にだけ余白が残って隣のカメラボタンが下に
      // 飛び出して見える（棚卸カウントの mb-3 等）。
      const MARGIN_RE = /^m[btxy]?-(?:0|1|2|3|4|5|auto)$/;
      Array.from(input.classList).forEach(function (c) {
        if (MARGIN_RE.test(c)) {
          input.classList.remove(c);
          group.classList.add(c);
        }
      });
      input.parentNode.insertBefore(group, input);
      group.appendChild(input);
    }
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-outline-primary hh-cam-btn';
    btn.setAttribute('aria-label', 'カメラでスキャン');
    btn.title = 'カメラでスキャン';
    btn.innerHTML = '<i class="bi bi-camera"></i>';
    btn.addEventListener('click', function () { openScanner(input); });
    input.insertAdjacentElement('afterend', btn);
  }

  // ===== スキャナーモーダル =====
  let modal, video, statusEl, detector;
  let currentInput = null;
  let stream = null;
  let rafHandle = null;
  let lastValue = '';     // 同じ値を連発しないためのデバウンス
  let lastValueAt = 0;

  // 誤読対策の合議（コンセンサス）。Code128 はチェックデジット内蔵で誤読は
  // デコーダが弾くが、ブレや隣接バーコードの一瞬の混入に備え、同じ値が連続
  // REQUIRED_HITS 回読めて初めて確定する（フレーム単位の取りこぼしを吸収）。
  const REQUIRED_HITS = 2;
  let candidate = '';     // 連続一致を数えている候補値
  let candidateHits = 0;

  function ensureModal() {
    if (modal) return;
    modal = document.createElement('div');
    modal.className = 'hh-cam-modal';
    modal.innerHTML = ''
      + '<div class="hh-cam-stage">'
      + '  <video class="hh-cam-video" autoplay playsinline muted></video>'
      + '  <div class="hh-cam-guide"></div>'
      + '  <div class="hh-cam-bar">'
      + '    <span class="hh-cam-status">スキャン待機中…</span>'
      + '    <button type="button" class="btn btn-light hh-cam-close">'
      + '      <i class="bi bi-x-lg me-1"></i>閉じる</button>'
      + '  </div>'
      + '</div>';
    document.body.appendChild(modal);
    video = modal.querySelector('.hh-cam-video');
    statusEl = modal.querySelector('.hh-cam-status');
    modal.querySelector('.hh-cam-close').addEventListener('click', closeScanner);
    // 背景タップで閉じる（プレビュー領域・コントロール領域以外）
    modal.addEventListener('click', function (e) {
      if (e.target === modal) closeScanner();
    });
  }

  function setStatus(text, kind) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.dataset.kind = kind || '';
  }

  async function openScanner(input) {
    ensureModal();
    currentInput = input;
    candidate = '';
    candidateHits = 0;
    setStatus('カメラを起動中…');
    modal.classList.add('open');
    document.body.classList.add('hh-cam-open');

    try {
      // 背面カメラ優先（無ければ前面でも可）。解像度はオートにブラウザに任せる
      stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: { ideal: 'environment' },
          width: { ideal: 1280 },
          height: { ideal: 720 },
        },
        audio: false,
      });
      video.srcObject = stream;
      await video.play();
      setStatus('バーコードを枠内に映してください');
      startDetection();
    } catch (err) {
      const name = err && err.name ? err.name : '';
      let msg = 'カメラを起動できませんでした';
      if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
        msg = 'カメラのアクセスが許可されていません。ブラウザの設定で許可してください。';
      } else if (name === 'NotFoundError' || name === 'OverconstrainedError') {
        msg = '利用可能なカメラが見つかりません。';
      } else if (err && err.message) {
        msg += ': ' + err.message;
      }
      setStatus(msg, 'error');
    }
  }

  function startDetection() {
    if (!detector) {
      detector = new window.BarcodeDetector({
        // このアプリが印刷するバーコードは Code128 のみ（ピッキングリスト・
        // 出荷明細書の指示番号など）。検出フォーマットを実際に印刷するものだけに
        // 絞ることが誤読対策の要。複数フォーマットを併用すると縞模様を別形式
        // として偽検出し、ゴミ値が返る。Code128 はチェックデジット内蔵なので
        // 誤読自体もデコーダ側で弾かれる。
        // 将来 JAN(EAN-13) ラベルを印刷・スキャンするなら 'ean_13' を足す。
        formats: ['code_128'],
      });
    }
    let busy = false;
    const loop = async function () {
      // 停止後の遅延コールバックを無視
      if (!stream) return;
      if (!busy && video.readyState >= 2 && video.videoWidth > 0) {
        busy = true;
        try {
          const results = await detector.detect(video);
          if (results && results.length > 0) {
            const value = (results[0].rawValue || '').trim();
            if (value) registerCandidate(value);
          }
        } catch (e) {
          // 検出途中の一時エラーは無視（連続検出で吸収される）
        }
        busy = false;
      }
      rafHandle = requestAnimationFrame(loop);
    };
    rafHandle = requestAnimationFrame(loop);
  }

  // 1 フレーム分の読み取り値を受け取り、同じ値が連続したら確定する。
  // 値が変わったら数え直し（誤読は連続しにくいので弾ける）。
  function registerCandidate(value) {
    if (value === candidate) {
      candidateHits += 1;
    } else {
      candidate = value;
      candidateHits = 1;
    }
    if (candidateHits < REQUIRED_HITS) {
      setStatus('読み取り中… (' + candidateHits + '/' + REQUIRED_HITS + ')');
      return;
    }
    onDetected(value);
  }

  function onDetected(text) {
    if (!currentInput) return;
    // 同じ値が短時間で連発するのを抑制
    const now = Date.now();
    if (text === lastValue && now - lastValueAt < 1500) return;
    lastValue = text;
    lastValueAt = now;

    setStatus('読み取り: ' + text, 'ok');
    if (navigator.vibrate) {
      try { navigator.vibrate(80); } catch (_) { /* iOS 等は無視 */ }
    }

    const input = currentInput;
    closeScanner();

    // 値を流し込み、既存の input ハンドラ・Enter ハンドラに処理させる
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

  function closeScanner() {
    if (rafHandle) { cancelAnimationFrame(rafHandle); rafHandle = null; }
    if (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
    }
    if (video) {
      video.srcObject = null;
    }
    if (modal) modal.classList.remove('open');
    document.body.classList.remove('hh-cam-open');
    currentInput = null;
    lastValue = '';
    lastValueAt = 0;
    candidate = '';
    candidateHits = 0;
  }

  // 端末の戻るボタン・Esc でも閉じる
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal && modal.classList.contains('open')) {
      e.preventDefault();
      closeScanner();
    }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectAll);
  } else {
    injectAll();
  }
})();
