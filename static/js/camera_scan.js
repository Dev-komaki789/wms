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
  // 起動世代トークン。getUserMedia は数秒かかることがあり、その間に閉じる/
  // 再起動されると、後から解決したストリームがカメラを掴みっぱなしになる
  // （次回起動時に黒画面）。openScanner / closeScanner で採番し、解決時に
  // 自分が最新世代でなければ取得済みストリームを即停止して破棄する。
  let openToken = 0;

  // 誤読対策の合議（コンセンサス）。Code128 はチェックデジット内蔵で誤読は
  // デコーダが弾くが、ブレや隣接バーコードの一瞬の混入に備え、同じ値が連続
  // REQUIRED_HITS 回読めて初めて確定する（フレーム単位の取りこぼしを吸収）。
  const REQUIRED_HITS = 2;
  let candidate = '';     // 連続一致を数えている候補値
  let candidateHits = 0;

  // 検出はガイド枠（CSS .hh-cam-guide）内に中心があるコードに限定する。
  // 視界の端に偶然映ったバーコードを誤って拾わないための対策。
  // GUIDE_W / GUIDE_H は CSS の .hh-cam-guide の幅・高さ比率と必ず一致させる。
  const GUIDE_W = 0.65;
  const GUIDE_H = 0.25;

  // BarcodeDetector の boundingBox は video.videoWidth × videoHeight（元映像
  // 解像度）の座標系。一方 CSS の .hh-cam-guide は表示エリア（video の
  // clientWidth × clientHeight）に対する比率で配置されている。
  // video は object-fit:cover で表示されるため元映像の一部だけが表示されて
  // いる状態で、両者の座標系は単純比例しない。
  // ここで「表示中に見えている元映像の領域」を逆算し、その中の中央
  // GUIDE_W × GUIDE_H をガイド枠とみなす。
  function isCenterInGuide(bb, video) {
    if (!bb || !video) return false;
    const vw = video.videoWidth, vh = video.videoHeight;
    const cw = video.clientWidth, ch = video.clientHeight;
    if (!vw || !vh || !cw || !ch) return false;
    // object-fit:cover のスケール（表示エリアを覆うように拡大）
    const scale = Math.max(cw / vw, ch / vh);
    // 元映像のうち、実際に画面に見えている領域（中央部のみ）
    const visibleW = cw / scale;
    const visibleH = ch / scale;
    const offsetX = (vw - visibleW) / 2;
    const offsetY = (vh - visibleH) / 2;
    // ガイド枠を元映像座標に変換（見えている領域の中央 GUIDE_W × GUIDE_H）
    const gw = visibleW * GUIDE_W;
    const gh = visibleH * GUIDE_H;
    const gx = offsetX + (visibleW - gw) / 2;
    const gy = offsetY + (visibleH - gh) / 2;
    const cx = bb.x + bb.width / 2;
    const cy = bb.y + bb.height / 2;
    return cx >= gx && cx <= gx + gw && cy >= gy && cy <= gy + gh;
  }

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

    const myToken = ++openToken;
    try {
      // 背面カメラ優先（無ければ前面でも可）。解像度はオートにブラウザに任せる
      const s = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: { ideal: 'environment' },
          width: { ideal: 1280 },
          height: { ideal: 720 },
        },
        audio: false,
      });
      // 起動待ちの間に閉じられた/別起動が始まっていたら、取得したストリームを
      // 即停止して破棄（カメラを掴みっぱなしにしない）。
      if (myToken !== openToken) {
        s.getTracks().forEach(function (t) { t.stop(); });
        return;
      }
      stream = s;
      video.srcObject = stream;
      await video.play();
      // play() 待ちの間に閉じられた場合も後始末する。
      if (myToken !== openToken) { closeScanner(); return; }
      setStatus('バーコードを枠内に映してください');
      startDetection();
    } catch (err) {
      // 閉じた後に届いたエラー（AbortError 等）は無視する。
      if (myToken !== openToken) return;
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
            // ガイド枠内に中心があるコードのみ採用。視界の端に映った別バーコードは
            // ここで弾く。複数が枠内なら最初の1つ。
            let value = '';
            for (const r of results) {
              if (isCenterInGuide(r.boundingBox, video)) {
                value = (r.rawValue || '').trim();
                if (value) break;
              }
            }
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
    // 起動中(getUserMedia 待ち)があれば無効化して、解決後に自己停止させる。
    openToken++;
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

  // ページ離脱・別アプリへの切替/画面ロックでカメラを確実に解放する。
  // モーダルを開いたままリロード/遷移すると closeScanner が呼ばれず、カメラを
  // 掴んだままになって次回起動時に黒画面になるのを防ぐ。
  window.addEventListener('pagehide', closeScanner);
  document.addEventListener('visibilitychange', function () {
    if (document.hidden && modal && modal.classList.contains('open')) {
      closeScanner();
    }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectAll);
  } else {
    injectAll();
  }
})();
