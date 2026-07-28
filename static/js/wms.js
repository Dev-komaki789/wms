/**
 * WMS 共通 JavaScript
 *
 * 全画面共通の UX:
 *  - フォーム表示時、最初の入力フィールドにオートフォーカス
 *  - Enter / ↓ で次のフィールド、↑ で前のフィールドにフォーカス移動
 *  - Enter 時は現在フィールドのバリデーションを実行
 *  - data-confirm 属性付きの送信ボタンで確認モーダル表示
 *  - 確認モーダル内の「はい」ボタンに自動フォーカス
 *
 * 設計: a/base.html を extends するすべての画面に自動適用される。
 * 規約: docs/画面仕様規約.md
 */

(function () {
  'use strict';

  // === ページ読み込み時の共通処理 ===
  window.addEventListener('DOMContentLoaded', function () {
    // 日付入力欄の年を4桁に制限する。
    // <input type="date"> は仕様上6桁年（最大275760年）まで許容するため、
    // min/max を与えて年を 2000〜9999 の範囲に収める。これをしないと
    // 100000 年のような値で検索でき、サーバ側の日付変換が例外になる。
    document.querySelectorAll('input[type="date"]').forEach(function (el) {
      if (!el.getAttribute('min')) el.min = '2000-01-01';
      if (!el.getAttribute('max')) el.max = '9999-12-31';
    });

    // <input type="number"> 共通の入力サニタイズ。
    // maxlength が効かないので、max 属性の桁数を上限にタイプ中に切り詰める。
    // 例: max="999999" の欄に「1234567」と打つと「123456」で止まる。
    // 999999 へのクリップ（値そのものを丸める）はしない。あくまで桁数制限。
    document.addEventListener('input', function (e) {
      const el = e.target;
      if (!el || el.tagName !== 'INPUT' || el.type !== 'number') return;
      const max = el.getAttribute('max');
      if (max == null || max === '') return;
      const maxLen = String(max).length;
      if (el.value.length > maxLen) {
        el.value = el.value.slice(0, maxLen);
      }
    });

    // 先頭ゼロの正規化は Enter キー / 離脱（blur）時のみ実行する。
    // 入力中（input イベント）には触らないので、000 や 001 と打っている途中も
    // そのまま見えて、確定操作で「0」「1」に整う。
    // 整数の先頭ゼロのみ対象。「0.5」「-1」など小数点・符号付きは触らない。
    function normalizeLeadingZero(el) {
      if (!el || el.tagName !== 'INPUT' || el.type !== 'number') return;
      const v = el.value;
      // 「0」「123」「100」「0.5」「-1」「空」などは何もしない。
      // 「0+数字以上」のときだけ整える（「0」単体は除外）。
      if (v === '' || v === '0' || !/^0+\d*$/.test(v)) return;
      const stripped = v.replace(/^0+/, '');
      el.value = stripped === '' ? '0' : stripped;
    }
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') normalizeLeadingZero(e.target);
    });
    document.addEventListener('focusout', function (e) {
      normalizeLeadingZero(e.target);
    });

    // 最初の入力フィールドに自動フォーカス。
    // 一覧画面や削除確認画面など、入力フィールドが無い場合は何もしない。
    const firstField = document.querySelector(
      'form input:not([type="hidden"]):not([disabled]):not([readonly]),'
      + 'form select:not([disabled]),'
      + 'form textarea:not([disabled])'
    );
    if (firstField) firstField.focus();
  });

  // === Enter / ↓ で次フィールド、↑ で前フィールド ===
  // textarea: Enter は改行 / 矢印は行移動なので何もしない
  // select: ↑↓/Enter は全て移動（誤操作で値が変わらないようマウスクリックで選択させる）
  // button[type=submit]: Enter は browser 既定 (=click → modal) / 矢印は移動
  // a.btn (キャンセル等): Enter はリンク遷移 / 矢印は移動
  document.addEventListener('keydown', function (e) {
    if (e.isComposing) return;
    const t = e.target;
    // input/button/select/textarea は .form プロパティを持つ
    // <a> は .form を持たないので closest('form') で親フォームを探す
    const form = t.form || (t.closest && t.closest('form'));
    if (!form) return;

    let direction = null;
    if (e.key === 'Enter')          direction = 1;
    else if (e.key === 'ArrowDown') direction = 1;
    else if (e.key === 'ArrowUp')   direction = -1;
    else return;

    // textarea: 全キー browser 既定
    if (t.tagName === 'TEXTAREA') return;
    // button: Enter は click、矢印は移動
    if ((t.tagName === 'BUTTON' || t.type === 'submit') && e.key === 'Enter') return;
    // a.btn (キャンセルリンク等): Enter はリンク遷移、矢印は移動
    if (t.tagName === 'A' && e.key === 'Enter') return;

    // Enter は「フィールドを確定」の意思表示 → 移動前に現在フィールドをバリデーション
    // 矢印キーは単なるナビゲーション → バリデーションしない（修正のため戻れる）
    if (e.key === 'Enter' && typeof t.checkValidity === 'function' && !t.checkValidity()) {
      e.preventDefault();
      t.reportValidity();   // ブラウザ標準の吹き出しエラー表示
      return;
    }

    e.preventDefault();
    // ナビゲーション対象: 入力フィールド + 送信ボタン + a.btn (キャンセル等)。
    // 検索モーダルを開く 🔍 等のユーティリティボタン(data-bs-toggle="modal")は
    // キーボード遷移の経路から外す。Enter で誤って検索モーダルが開くのを防ぐ。
    const focusables = Array.from(form.querySelectorAll(
      'input, select, textarea, button, a.btn'
    )).filter(f => !f.disabled && f.type !== 'hidden' && f.offsetParent !== null
      && !(f.tagName === 'BUTTON' && f.getAttribute('data-bs-toggle') === 'modal'));
    const idx = focusables.indexOf(t);
    const next = focusables[idx + direction];
    if (next) next.focus();
  });

  // === select で値を選んだら次フィールドに自動遷移 ===
  // ユーザーがマウスで選択した瞬間に focus 移動。change イベントは値が変化した時のみ発火するので
  // 同じ値を選び直しても何も起きない（意図通り）。
  document.addEventListener('change', function (e) {
    const t = e.target;
    if (t.tagName !== 'SELECT') return;
    const form = t.form;
    if (!form) return;
    const focusables = Array.from(form.querySelectorAll(
      'input, select, textarea, button, a.btn'
    )).filter(f => !f.disabled && f.type !== 'hidden' && f.offsetParent !== null
      && !(f.tagName === 'BUTTON' && f.getAttribute('data-bs-toggle') === 'modal'));
    const idx = focusables.indexOf(t);
    const next = focusables[idx + 1];
    if (next) next.focus();
  });

  // === 確認モーダルが開いたら「はい」ボタンに自動フォーカス（Enterで即確認可能） ===
  const confirmModalEl = document.getElementById('confirmModal');
  if (confirmModalEl) {
    confirmModalEl.addEventListener('shown.bs.modal', function () {
      const yesBtn = document.getElementById('confirmModalYes');
      if (yesBtn) yesBtn.focus();
    });
  }

  // === data-confirm 付きの送信ボタンは確認モーダルを表示 ===
  // 使い方: <button type="submit" data-confirm="この内容で登録しますか？">保存</button>
  // オプション:
  //   data-confirm-variant="danger"  はいボタンの色 (primary|danger|warning|...)
  //   data-confirm-yes="削除する"     はいボタンのラベル
  document.addEventListener('click', function (e) {
    const btn = e.target.closest('[data-confirm]');
    if (!btn) return;
    const form = btn.form;
    if (!form) return;
    e.preventDefault();

    // HTML5 バリデーションを先に実行
    // checkValidity() = required/pattern等を全部チェック
    // reportValidity() = エラーがあるフィールドにフォーカス + ツールチップ表示
    if (!form.checkValidity()) {
      form.reportValidity();
      return;
    }

    const message = btn.dataset.confirm || '実行しますか？';
    const variant = btn.dataset.confirmVariant || 'primary';
    const yesLabel = btn.dataset.confirmYes || 'はい';

    document.getElementById('confirmModalBody').textContent = message;
    const yesBtn = document.getElementById('confirmModalYes');
    yesBtn.className = 'btn btn-' + variant;
    yesBtn.textContent = yesLabel;
    yesBtn.onclick = function () {
      // hidden input でどのボタンが押されたか送信
      if (btn.name) {
        const hidden = document.createElement('input');
        hidden.type = 'hidden';
        hidden.name = btn.name;
        hidden.value = btn.value || '';
        form.appendChild(hidden);
      }
      // requestSubmit() = 送信前にもう一度バリデーション。submit()はスキップしてしまう
      if (form.requestSubmit) {
        form.requestSubmit();
      } else {
        form.submit();   // 古いブラウザfallback
      }
    };
    new bootstrap.Modal(document.getElementById('confirmModal')).show();
  });
})();

// === ハンディ画面用: プログラム的に確認モーダルを表示するヘルパ ===
// form submit ベースの data-confirm では対応できない fetch ベースの per-item
// コミット（棚入れ・ピッキング・出荷検品）で使う。
// 使い方: wmsConfirm('実行しますか？', {variant: 'primary', yesLabel: '実行'}, () => {...});
window.wmsConfirm = function (message, opts, onYes) {
  opts = opts || {};
  if (typeof opts === 'function') { onYes = opts; opts = {}; }
  const modalEl = document.getElementById('confirmModal');
  if (!modalEl || typeof bootstrap === 'undefined') {
    // モーダル基盤が無ければそのまま実行（degradation）
    if (onYes) onYes();
    return;
  }
  document.getElementById('confirmModalBody').textContent = message;
  const yesBtn = document.getElementById('confirmModalYes');
  yesBtn.className = 'btn btn-' + (opts.variant || 'primary') + ' px-4';
  yesBtn.textContent = opts.yesLabel || '実行する';
  const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
  yesBtn.onclick = function () {
    modal.hide();
    if (onYes) onYes();
  };
  modal.show();
};

// === フォーム二重送信ガード ===
// 送信が始まったら送信ボタンを無効化＋スピナー表示し、二度押しによる
// 二重送信（同じ CSV を2回取り込む等）を防ぐ。ネイティブ送信（ページ遷移）専用。
// data-confirm 経由（requestSubmit）でも submit イベントが発火するので同じく効く。
// AJAX（fetch）系はそもそも form submit しないため影響しない。
// 例外にしたいフォームは <form data-no-submit-guard> を付ける。
(function () {
  'use strict';

  function releaseButton(btn) {
    btn.disabled = false;
    if (btn.dataset.guardOriginalHtml != null) {
      btn.innerHTML = btn.dataset.guardOriginalHtml;
      delete btn.dataset.guardOriginalHtml;
    }
  }

  document.addEventListener('submit', function (e) {
    const form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.hasAttribute('data-no-submit-guard')) return;

    // すでに送信中なら2回目以降を止める
    if (form.dataset.submitting === '1') {
      e.preventDefault();
      return;
    }
    form.dataset.submitting = '1';

    // 送信ボタン（button/input）を無効化。button はスピナー付き「処理中…」に差し替え。
    // 無効化は送信データ確定後に行われるため、送信自体はそのまま実行される。
    form.querySelectorAll(
      'button[type="submit"]:not([disabled]), input[type="submit"]:not([disabled])'
    ).forEach(function (btn) {
      if (btn.tagName === 'BUTTON') {
        btn.dataset.guardOriginalHtml = btn.innerHTML;
        btn.innerHTML =
          '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>処理中…';
      }
      btn.disabled = true;
    });
  });

  // 「戻る」ボタンで bfcache から復元されたページはボタンが無効のまま残るので解除する。
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    document.querySelectorAll('form[data-submitting="1"]').forEach(function (form) {
      form.dataset.submitting = '0';
      form
        .querySelectorAll('button[type="submit"], input[type="submit"]')
        .forEach(releaseButton);
    });
  });
})();
