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

    // <input type="number"> 共通の入力サニタイズ:
    //   (a) max 属性を超える値は即座にクリップ（maxlength が効かないため）
    //   (b) 先頭ゼロを正規化（"000"→"0", "0123"→"123"）
    //       「0」を連発して見かけ上の桁数を増やせるのを防ぐ。
    // programmatic な value 代入は input イベントを再発火させない仕様なので
    // 無限ループにはならない。
    document.addEventListener('input', function (e) {
      const el = e.target;
      if (!el || el.tagName !== 'INPUT' || el.type !== 'number') return;

      // (a) max 超過クリップ
      const max = el.getAttribute('max');
      if (max != null && max !== '') {
        const maxNum = Number(max);
        if (Number.isFinite(maxNum)) {
          const v = el.value;
          if (v && Number(v) > maxNum) {
            el.value = String(maxNum);
          }
        }
      }

      // (b) 先頭ゼロの正規化
      const v = el.value;
      if (/^0\d/.test(v)) {
        // 「0123」「001」など → 先頭ゼロを全部消す
        el.value = v.replace(/^0+/, '');
      } else if (/^0{2,}$/.test(v)) {
        // 「000」など 0 の連続のみ → 「0」1つに
        el.value = '0';
      }
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
