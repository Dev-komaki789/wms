/**
 * ハンディ（handheld）入力フォーム用スキャン補助。
 *
 * 対象: <form data-hh-scan ...>（計画外入庫/出庫・棚間移動など）
 *  - Enter（スキャナーの送信）で次フィールドへ。最後で実行ボタンへフォーカス。
 *  - data-hh-lookup のフィールドは Enter 時にマスタ API で「その場で」検証する:
 *      "location" 棚番の実在
 *      "sku"      SKU の実在
 *      "stock"    棚番 × SKU の在庫紐づき（その棚に在庫があるか）。
 *                 対象の棚番フィールドの id を data-hh-stock-loc で指定する。
 *    検証 NG ならその場でエラー表示し、前進・送信を止める。
 *  - data-hh-require-loc を持つフィールド: 検証より前に、指定 id の棚番欄が
 *    空でないことを要求する。空なら「先にロケーションを入力してください。」と
 *    エラーにして API も呼ばない。業務フローを棚番先行に揃えるために使う
 *    （計画外入庫・棚卸カウントなど、SKU 単体 API でも入力順を縛りたい場面）。
 *  - Enter は本スクリプトが占有し、wms.js のフィールド移動には渡さない
 *    （stopPropagation）。矢印キーは従来どおり wms.js が処理する。
 *
 * フォーム属性: data-loc-api / data-sku-api / data-stock-api に各 API の URL。
 * 結果表示先: <div class="hh-scan-msg" data-for="<input の id>">
 *
 * 3層バリデーションのフロント層（UX）。サーバ側のフォーム検証は別途残す。
 */
(function () {
  'use strict';

  const form = document.querySelector('form[data-hh-scan]');
  if (!form) return;

  const API = {
    location: form.dataset.locApi || '',
    sku: form.dataset.skuApi || '',
    stock: form.dataset.stockApi || '',
  };
  const NOUN = {location: '棚番', sku: 'SKU'};

  const fields = Array.from(form.querySelectorAll(
    'input:not([type=hidden]):not([disabled]), select:not([disabled])'
  ));
  const submitBtn = form.querySelector('button[type=submit]');
  // lookup フィールドの検証状態: input.id -> 'ok' | 'ng' | undefined（未検証）
  const state = {};

  function msgBox(input) {
    return form.querySelector('.hh-scan-msg[data-for="' + input.id + '"]');
  }

  function setMsg(input, text, ok) {
    const el = msgBox(input);
    if (!el) return;
    el.textContent = text || '';
    el.className = 'hh-scan-msg small'
      + (text ? (' mt-1 ' + (ok ? 'text-success' : 'text-danger')) : '');
  }

  function focusNext(idx) {
    const next = fields[idx + 1];
    if (next) next.focus();
    else if (submitBtn) submitBtn.focus();
  }

  function clearState(input) {
    state[input.id] = undefined;
    setMsg(input, '', true);
  }

  function fail(input, msg) {
    state[input.id] = 'ng';
    setMsg(input, msg, false);
    // 振動でフィードバック（Android のみ。iOS Safari は非対応）。
    // サーバー側エラーより控えめに 2回。handheld_base.html の messages 系と差をつける。
    if ('vibrate' in navigator) navigator.vibrate([80, 60, 80]);
    input.focus();
    input.select();
  }

  // fetch して evaluate(data) -> {ok, msg} の結果を表示する
  function request(input, idx, url, evaluate) {
    input.disabled = true;
    setMsg(input, '確認中…', true);
    // credentials: same-origin を明示（一部の環境で AJAX に session cookie が
    // 乗らないケースの保険）。type=json を要求する Accept ヘッダも付ける。
    fetch(url, {
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json',
      },
      credentials: 'same-origin',
    })
      .then(function (res) {
        // API 側は JsonAuthRequiredMixin により未認証時に 401 + JSON を返す。
        // 念のため Content-Type も見て、HTML が返ってきたケースも捕捉する。
        if (res.status === 401) {
          throw new Error('SESSION_EXPIRED|status=401|cause=auth');
        }
        const ct = res.headers.get('content-type') || '(none)';
        if (!ct.includes('application/json')) {
          throw new Error('SESSION_EXPIRED|status=' + res.status + '|ct=' + ct + '|url=' + res.url);
        }
        return res.json();
      })
      .then(function (data) {
        input.disabled = false;
        const r = evaluate(data);
        if (r.ok) {
          state[input.id] = 'ok';
          setMsg(input, r.msg, true);
          focusNext(idx);
        } else {
          fail(input, r.msg);
        }
      })
      .catch(function (err) {
        input.disabled = false;
        state[input.id] = undefined;
        let msg = '通信エラーが発生しました。もう一度お試しください。';
        if (err && err.message && err.message.startsWith('SESSION_EXPIRED')) {
          // デバッグ用の付加情報をそのまま表示
          msg = 'ログインが切れています。詳細: ' + err.message;
        } else if (err && err.message) {
          msg = '通信エラー: ' + err.message;
        }
        setMsg(input, msg, false);
        input.focus();
        input.select();
      });
  }

  // 棚番 / SKU の単純な実在チェックの結果評価
  function existsResult(kind, code, data) {
    if (!data.found) {
      return {ok: false,
              msg: NOUN[kind] + '「' + code + '」は存在しないか、無効です。'};
    }
    const detail = kind === 'sku'
      ? (data.sku_code + '（' + (data.product_name || '') + '）')
      : (data.location_code + '（' + (data.area_name || '') + '）');
    return {ok: true, msg: '✓ ' + detail};
  }

  // 棚番 × SKU の在庫紐づきチェックの結果評価
  function stockResult(code, data) {
    if (!data.sku_found) {
      return {ok: false, msg: 'SKU「' + code + '」は存在しないか、無効です。'};
    }
    if (!data.location_found) {
      return {ok: false,
              msg: 'ロケーションが無効です。先に正しく入力してください。'};
    }
    if ((data.on_hand || 0) <= 0) {
      return {ok: false, msg: 'ロケーション「' + data.location_code + '」に SKU「'
              + data.sku_code + '」の在庫がありません（在庫 0）。'};
    }
    return {ok: true, msg: '✓ ' + data.sku_code + '（'
            + (data.product_name || '') + '）／在庫 ' + data.on_hand};
  }

  function lookup(input, idx) {
    const kind = input.dataset.hhLookup;
    const code = input.value.trim();
    if (!code) { focusNext(idx); return; }

    // 棚番先行を要求するフラグ。指定 id の棚番欄が空ならここで弾く。
    // 計画外入庫・棚卸カウントなど SKU 単体 API でも棚番先行を強制したい画面で使う。
    const requireLocId = input.dataset.hhRequireLoc;
    if (requireLocId) {
      const locInput = document.getElementById(requireLocId);
      const locCode = locInput ? locInput.value.trim() : '';
      if (!locCode) {
        fail(input, '先にロケーションを入力してください。');
        return;
      }
    }

    if (kind === 'stock') {
      // 対象の棚番フィールドの値と組み合わせて在庫をチェックする
      const locInput = document.getElementById(input.dataset.hhStockLoc || '');
      const locCode = locInput ? locInput.value.trim() : '';
      if (!locCode) {
        fail(input, '先にロケーションを入力してください。');
        return;
      }
      if (!API.stock) { focusNext(idx); return; }
      request(input, idx,
              API.stock + '?location=' + encodeURIComponent(locCode)
              + '&sku=' + encodeURIComponent(code),
              function (data) { return stockResult(code, data); });
      return;
    }

    const api = API[kind];
    if (!api) { focusNext(idx); return; }
    request(input, idx, api + '?code=' + encodeURIComponent(code),
            function (data) { return existsResult(kind, code, data); });
  }

  fields.forEach(function (input, idx) {
    if (input.dataset.hhLookup) {
      // 入力が変わったら前回の検証結果を捨てる（古い ✓/エラーを残さない）。
      // 棚番が変わったら、それに依存する在庫チェック欄の結果もクリアする。
      input.addEventListener('input', function () {
        clearState(input);
        form.querySelectorAll('[data-hh-stock-loc="' + input.id + '"]')
            .forEach(clearState);
      });
    }
    input.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      // Enter は本スクリプトが占有（wms.js のフィールド移動には渡さない）
      e.preventDefault();
      e.stopPropagation();
      if (typeof input.checkValidity === 'function' && !input.checkValidity()) {
        input.reportValidity();
        return;
      }
      if (input.dataset.hhLookup) {
        lookup(input, idx);
      } else {
        focusNext(idx);
      }
    });
    // 数値フィールドはフォーカス時に全選択（打ち直しが楽）
    if (input.inputMode === 'numeric') {
      input.addEventListener('focus', function () { this.select(); });
    }
  });

  // 送信時: 棚番 / SKU / 在庫紐づき が NG のままなら送信を止める
  form.addEventListener('submit', function (e) {
    for (let i = 0; i < fields.length; i++) {
      const input = fields[i];
      if (input.dataset.hhLookup && state[input.id] === 'ng') {
        e.preventDefault();
        input.focus();
        input.select();
        return;
      }
    }
  });
})();
