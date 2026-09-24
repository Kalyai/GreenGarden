/* ============================================================
   «Зелёный дворик» — интерактивность сайта.

   Блоки выстроены по зависимостям: сначала утилиты и общие
   механизмы (тост, маска, заявки, модалки), затем фичи, которые
   ими пользуются (корзина, шапка, фильтры, слайдер, загрузка).
   Поведение описано данными разметки: data-атрибуты вместо id,
   где элемент повторяется (кнопки корзины, вкладки, модалки).
   ============================================================ */
(() => {
  "use strict";

  /* ---------- 0. Утилиты и настройки ---------- */
  const $ = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];

  const CONFIG = {
    scrollShadowPx: 8,   // порог тени шапки при скролле
    autoplayMs: 5000,    // период автопрокрутки слайдера
    toastMs: 3000,       // время жизни тоста
    swipePx: 40,         // минимальная длина свайпа для листания
    quantityMax: 999,    // предел количества товара в корзине
  };

  // Экранирование значений, которые попадают в HTML-строку. Имена товаров
  // приходят из каталога и из localStorage, то есть не полностью доверены:
  // без экранирования кавычка или тег в имени становились исполняемым кодом.
  const escapeHtml = (value) =>
    String(value).replace(/[&<>"']/g, (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

  // Момент, когда форма стала доступна пользователю. Бэкенд отклоняет
  // отправку быстрее двух секунд — человек за это время форму не заполнит.
  const formOpenedAt = new WeakMap();
  const markFormsOpened = (ctx = document) =>
    $$("form", ctx).forEach((form) => formOpenedAt.set(form, Date.now()));

  /* ---------- 1. Тост-уведомления ---------- */
  const toast = $("#toast");
  let toastTimer = null;

  const showToast = (message) => {
    toast.textContent = message;
    toast.classList.add("is-visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("is-visible"), CONFIG.toastMs);
  };

  /* ---------- 2. Маска телефона +7 (___) ___-__-__ ---------- */
  const formatPhone = (raw) => {
    let digits = raw.replace(/\D/g, "");
    if (digits.startsWith("8")) digits = "7" + digits.slice(1);
    if (digits && !digits.startsWith("7")) digits = "7" + digits;
    digits = digits.slice(0, 11);

    let out = "+7";
    if (digits.length > 1) out += " (" + digits.slice(1, 4);
    if (digits.length >= 5) out += ") " + digits.slice(4, 7);
    if (digits.length >= 8) out += "-" + digits.slice(7, 9);
    if (digits.length >= 10) out += "-" + digits.slice(9, 11);
    return out;
  };

  $$("input[name='phone']").forEach((input) =>
    input.addEventListener("input", (e) => {
      e.target.value = formatPhone(e.target.value);
      e.target.classList.remove("is-invalid");
    })
  );

  /* ---------- 3. Заявки: сайт → бэкенд → Telegram-бот ---------- */
  const sendLead = (payload) =>
    fetch("/api/lead", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

  const validateLeadForm = (form) => {
    let valid = true;
    if (!form.elements.name.value.trim()) {
      form.elements.name.classList.add("is-invalid");
      valid = false;
    }
    if (form.elements.phone.value.replace(/\D/g, "").length < 11) {
      form.elements.phone.classList.add("is-invalid");
      valid = false;
    }
    return valid;
  };

  // Ошибка гаснет, как только пользователь правит поле
  $$("input[name='name'], input[name='phone']").forEach((input) =>
    input.addEventListener("input", () => input.classList.remove("is-invalid"))
  );

  /* Согласие на обработку ПД (152-ФЗ): кнопка отправки неактивна,
     пока чекбокс не отмечен; после сброса формы согласие снимается. */
  const bindConsent = (form) => {
    const box = form.elements.consent;
    const btn = form.querySelector("[type='submit']");
    if (!box || !btn) return;
    const sync = () => { btn.disabled = !box.checked; };
    box.addEventListener("change", sync);
    form.addEventListener("reset", () => setTimeout(sync, 0));
    sync();
  };
  $$("form").forEach(bindConsent);
  markFormsOpened();   // формы вне модалок доступны с момента загрузки

  /* Отправка одной формы-заявки. Возвращает true при успехе.
     Кнопка на время запроса блокируется и показывает «Отправляем…». */
  const submitLead = async (form, source, extra = {}) => {
    const btn = form.querySelector("[type='submit']");
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Отправляем…";
    try {
      const res = await sendLead({
        name: form.elements.name.value.trim(),
        phone: form.elements.phone.value,
        comment: form.elements.comment ? form.elements.comment.value.trim() : "",
        consent: Boolean(form.elements.consent?.checked),
        // honeypot: поле скрыто от человека, но боты его заполняют
        website: form.elements.website ? form.elements.website.value : "",
        // сколько миллисекунд форма была открыта до отправки
        elapsed_ms: Date.now() - (formOpenedAt.get(form) || Date.now()),
        source,
        ...extra,
      });
      if (!res.ok) throw new Error(res.status);
      return true;
    } catch {
      return false;
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  };

  /* ---------- 4. Модальные окна ---------- */
  /* Фабрика: открытие/закрытие, фокус туда-обратно, блокировка
     скролла и сброс состояния через onClose. */
  const createModal = (root, closeSelector, onClose) => {
    // страницы без этой модалки (например, catalog.html без колбэка)
    if (!root) return { root: null, open() {}, close() {}, get isOpen() { return false; } };
    let lastFocused = null;

    const open = () => {
      lastFocused = document.activeElement;
      root.hidden = false;
      document.body.classList.add("modal-open");
      markFormsOpened(root);   // отсчёт времени заполнения начинается заново
      $(".form__input", root)?.focus({ preventScroll: true });
    };
    const close = () => {
      root.hidden = true;
      document.body.classList.remove("modal-open");
      onClose?.();
      // preventScroll: возврат фокуса на кнопку в шапке не должен
      // прокручивать страницу наверх
      lastFocused?.focus({ preventScroll: true });
    };

    $$(closeSelector, root).forEach((btn) =>
      btn.addEventListener("click", close)
    );
    return { root, open, close, get isOpen() { return !root.hidden; } };
  };

  /* ---------- 5. Корзина ----------
     Состояние сохраняется в localStorage: корзина переживает переходы
     между главной и каталогом и синхронизируется между вкладками. */
  const CART_KEY = "gd-cart";
  const cart = new Map(); // name → { name, price, quantity }
  const cartCountEl = $("#cartCount");

  const clampQty = (n) =>
    Math.max(1, Math.min(CONFIG.quantityMax, Number.parseInt(n, 10) || 1));

  const saveCart = () => {
    try {
      localStorage.setItem(CART_KEY, JSON.stringify(cartItems()));
    } catch { /* приватный режим: просто не сохраняем */ }
  };

  const loadCart = () => {
    try {
      const saved = JSON.parse(localStorage.getItem(CART_KEY) || "[]");
      if (!Array.isArray(saved)) return;
      saved.forEach((item) => {
        if (!item || !item.name) return;
        cart.set(String(item.name), {
          name: String(item.name),
          price: String(item.price || ""),
          quantity: clampQty(item.quantity),
        });
      });
    } catch { /* повреждённые данные игнорируем */ }
  };

  const clearCart = () => {
    cart.clear();
    saveCart();
    renderCart();
  };

  const cartTotal = () =>
    [...cart.values()].reduce((sum, item) => sum + item.quantity, 0);
  const cartItems = () =>
    [...cart.values()].map(({ name, price, quantity }) => ({ name, price, quantity }));

  const bumpCartCount = () => {
    cartCountEl.classList.remove("is-bump");
    void cartCountEl.offsetWidth; // reflow перезапускает анимацию
    cartCountEl.classList.add("is-bump");
  };

  // Строка позиции в модалках: название, цена и управление количеством.
  // Создаётся один раз на позицию, дальше обновляется только значение,
  // чтобы не терять фокус при ручном вводе.
  const orderRow = (item) => {
    const row = document.createElement("div");
    row.className = "cart-order__item";
    row.dataset.name = item.name;
    const details = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = item.name;
    const price = document.createElement("span");
    price.textContent = item.price;
    details.append(name, price);
    row.append(details);
    row.insertAdjacentHTML("beforeend", `
      <div class="card__quantity card__quantity--full">
        <button type="button" class="card__quantity-btn" data-quantity-minus aria-label="Уменьшить количество ${escapeHtml(item.name)}">−</button>
        <input type="number" inputmode="numeric" min="1" max="${CONFIG.quantityMax}" value="${escapeHtml(item.quantity)}" aria-label="Количество: ${escapeHtml(item.name)}">
        <button type="button" class="card__quantity-btn" data-quantity-plus aria-label="Увеличить количество ${escapeHtml(item.name)}">+</button>
        <button type="button" class="card__quantity-btn card__quantity-btn--remove" data-remove-item aria-label="Убрать ${escapeHtml(item.name)} из корзины">
          <svg><use href="#icon-trash"/></svg>
        </button>
      </div>`);
    return row;
  };

  const callbackCartList = $("#callbackCartItems");
  const cartOrderList = $("#cartOrderItems");
  const orderLists = [callbackCartList, cartOrderList].filter(Boolean);

  const renderOrderLists = () => {
    const items = cartItems();
    orderLists.forEach((list) => {
      $$(".cart-order__item", list).forEach((row) => {
        if (!cart.has(row.dataset.name)) row.remove();
      });
      items.forEach((item) => {
        let row = $(`.cart-order__item[data-name="${CSS.escape(item.name)}"]`, list);
        if (!row) {
          row = orderRow(item);
          list.append(row);
        }
        $("input", row).value = item.quantity;
      });
    });
    callbackCartList.hidden = !items.length;
    cartOrderList.hidden = !items.length;
  };

  const renderCartButtons = () => {
    const total = cartTotal();
    cartCountEl.textContent = total;
    cartCountEl.hidden = total === 0;
    $$("[data-add-to-cart]").forEach((btn) => {
      const item = cart.get(btn.dataset.name);
      const controls = btn.parentElement.querySelector("[data-quantity-controls]");
      btn.hidden = Boolean(item);
      controls.hidden = !item;
      if (item) $("input", controls).value = item.quantity;
    });
  };

  const renderCart = () => {
    renderCartButtons();
    renderOrderLists();
  };

  const cartModal = createModal($("#cartModal"), "[data-close-cart-modal]", () => {
    $("#cartOrderFormWrap").hidden = false;
    $("#cartModalSuccess").hidden = true;
    $("#cartOrderForm").reset();
  });

  const setQuantity = (name, quantity, announce = false) => {
    const item = cart.get(name);
    if (!item) return;
    const safe = Math.max(0, Math.min(CONFIG.quantityMax, Number.parseInt(quantity, 10) || 0));
    if (safe === 0) cart.delete(name);
    else item.quantity = safe;
    renderCart();
    saveCart();
    if (announce) bumpCartCount();
  };

  const removeFromCart = (name) => {
    cart.delete(name);
    renderCart();
    saveCart();
  };

  // Управление количеством и удаление прямо в списках заказа
  orderLists.forEach((list) => {
    list.addEventListener("click", (e) => {
      const row = e.target.closest(".cart-order__item");
      const item = row && cart.get(row.dataset.name);
      if (!item) return;
      if (e.target.closest("[data-quantity-minus]")) setQuantity(item.name, item.quantity - 1, true);
      else if (e.target.closest("[data-quantity-plus]")) setQuantity(item.name, item.quantity + 1, true);
      else if (e.target.closest("[data-remove-item]")) removeFromCart(item.name);
    });
    list.addEventListener("input", (e) => {
      const row = e.target.closest(".cart-order__item");
      if (row && e.target.matches("input") && e.target.value !== "") {
        setQuantity(row.dataset.name, e.target.value, true);
      }
    });
  });

  // В каждой карточке: кнопка «В корзину» ↔ счётчик количества
  $$("[data-add-to-cart]").forEach((btn) => {
    const name = btn.dataset.name;
    const price = $(".card__price", btn.closest(".card")).textContent.trim();

    const controls = document.createElement("div");
    controls.className = "card__quantity";
    controls.dataset.quantityControls = "";
    controls.hidden = true;
    controls.innerHTML = `
      <button type="button" class="card__quantity-btn" data-quantity-minus aria-label="Уменьшить количество ${escapeHtml(name)}">−</button>
      <input type="number" inputmode="numeric" min="1" max="${CONFIG.quantityMax}" value="1" aria-label="Количество: ${escapeHtml(name)}">
      <button type="button" class="card__quantity-btn" data-quantity-plus aria-label="Увеличить количество ${escapeHtml(name)}">+</button>`;
    btn.before(controls);

    btn.addEventListener("click", () => {
      cart.set(name, { name, price, quantity: 1 });
      renderCart();
      saveCart();
      bumpCartCount();
      showToast(`«${name}» добавлен в корзину`);
    });
    $("[data-quantity-minus]", controls).addEventListener("click", () =>
      setQuantity(name, cart.get(name).quantity - 1, true)
    );
    $("[data-quantity-plus]", controls).addEventListener("click", () =>
      setQuantity(name, cart.get(name).quantity + 1, true)
    );
    const quantityInput = $("input", controls);
    const syncQuantity = (e) => {
      if (e.target.value !== "") setQuantity(name, e.target.value, true);
    };
    quantityInput.addEventListener("input", syncQuantity);
    quantityInput.addEventListener("change", syncQuantity);
  });

  // «Оформить заказ»: внутри корзина и форма связи,
  // при пустой корзине списки скрыты и остаётся только форма
  $("#checkoutBtn")?.addEventListener("click", () => cartModal.open());

  // восстановление корзины после перехода между страницами
  loadCart();
  renderCart();

  // изменения из другой вкладки
  window.addEventListener("storage", (e) => {
    if (e.key !== CART_KEY) return;
    cart.clear();
    loadCart();
    renderCart();
  });

  /* ---------- 6. Форма «Заказать звонок» ---------- */
  const callbackModal = createModal($("#callbackModal"), "[data-close-modal]", () => {
    const form = $("#callbackForm");
    form.hidden = false;
    $("#modalSuccess").hidden = true;
    form.reset();
  });

  $$("[data-open-modal]").forEach((btn) =>
    btn.addEventListener("click", () => {
      renderOrderLists(); // показать текущую корзину, если она есть
      callbackModal.open();
    })
  );

  $("#callbackForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    if (!validateLeadForm(form)) return;
    const ok = await submitLead(form, "modal", cart.size ? { items: cartItems() } : {});
    if (ok) {
      form.hidden = true;
      $("#modalSuccess").hidden = false;
    } else {
      showToast("Не удалось отправить заявку — позвоните нам, пожалуйста");
    }
  });

  /* ---------- 7. Оформление заказа из корзины ---------- */
  $("#cartOrderForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    if (!validateLeadForm(form) || !cart.size) return;
    const ok = await submitLead(form, "cart", { items: cartItems() });
    if (ok) {
      clearCart();
      $("#cartOrderFormWrap").hidden = true;
      $("#cartModalSuccess").hidden = false;
    } else {
      showToast("Не удалось отправить заказ — позвоните нам, пожалуйста");
    }
  });

  /* ---------- 8. Форма в CTA-блоке (есть только на главной) ---------- */
  const ctaForm = $("#ctaForm");
  const ctaStatus = $("#ctaFormStatus");

  ctaForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    ctaStatus.hidden = true;
    if (!validateLeadForm(ctaForm)) {
      ctaStatus.textContent = "Проверьте имя и телефон: нужен формат +7 (___) ___-__-__";
      ctaStatus.classList.remove("is-ok");
      ctaStatus.hidden = false;
      return;
    }
    const ok = await submitLead(ctaForm, "cta");
    if (ok) {
      ctaForm.reset();
      ctaStatus.textContent = "Заявка ушла в Telegram — агроном перезвонит в часы работы питомника.";
      ctaStatus.classList.add("is-ok");
    } else {
      ctaStatus.textContent = "Не удалось отправить заявку. Позвоните: +7 (925) 881-01-90";
      ctaStatus.classList.remove("is-ok");
    }
    ctaStatus.hidden = false;
  });

  /* Escape закрывает любую открытую модалку */
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (callbackModal.isOpen) callbackModal.close();
    if (cartModal.isOpen) cartModal.close();
  });

  /* ---------- 9. Шапка: тень при скролле ---------- */
  const header = $("#header");
  const onScroll = () => {
    header.classList.toggle("is-scrolled", window.scrollY > CONFIG.scrollShadowPx);
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  /* ---------- 10. Мобильное меню ---------- */
  const burger = $("#burger");
  const nav = $("#nav");

  const closeMenu = () => {
    nav.classList.remove("is-open");
    burger.classList.remove("is-open");
    burger.setAttribute("aria-expanded", "false");
  };

  burger.addEventListener("click", () => {
    const isOpen = nav.classList.toggle("is-open");
    burger.classList.toggle("is-open", isOpen);
    burger.setAttribute("aria-expanded", String(isOpen));
  });

  $$(".nav__link").forEach((link) => link.addEventListener("click", closeMenu));

  /* ---------- 11. Фильтры каталога ---------- */
  const filterChips = $$(".catalog__filters .chip");
  const productCards = $$(".catalog__grid .card");

  filterChips.forEach((chip) => {
    chip.addEventListener("click", () => {
      filterChips.forEach((c) => c.classList.remove("is-active"));
      chip.classList.add("is-active");

      const filter = chip.dataset.filter;
      productCards.forEach((card) => {
        const tags = (card.dataset.tags || "").split(/\s+/);
        card.classList.toggle("is-hidden", filter !== "all" && !tags.includes(filter));
      });
    });
  });

  // catalog.html#conifer и т.п.: фильтр включается по хэшу в адресе
  const presetChip = filterChips.find((c) => c.dataset.filter === location.hash.slice(1));
  if (presetChip) presetChip.click();

  /* ---------- 12. Появление блоков при скролле ---------- */
  const revealEls = $$(".reveal");
  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-visible");
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -40px 0px" }
    );
    revealEls.forEach((el) => observer.observe(el));
  } else {
    revealEls.forEach((el) => el.classList.add("is-visible"));
  }

  /* ---------- 13. Запасные изображения ----------
     Если файл не загрузился, подставляем адрес из data-fallback. */
  $$("img[data-fallback]").forEach((img) => {
    img.addEventListener("error", () => {
      if (img.dataset.fallbackApplied) return;
      img.dataset.fallbackApplied = "1";
      img.src = img.dataset.fallback;
    }, { once: true });
  });

  /* ---------- 14. Слайдер в шапке ----------
     Автопрокрутка, стрелки, вкладки, свайп. Пауза при фокусе
     внутри слайдера и на скрытой вкладке браузера. */
  const slider = $("#heroSlider");
  if (slider) {
    const slides = $$(".hero__slide", slider);
    const tabs = $$(".hero__tab", slider);
    const counter = $("#heroCurrent");
    let current = 0;
    let timer = null;

    const goTo = (index) => {
      current = (index + slides.length) % slides.length;
      slides.forEach((slide, i) => slide.classList.toggle("is-active", i === current));
      tabs.forEach((tab, i) => {
        tab.classList.toggle("is-active", i === current);
        tab.setAttribute("aria-current", String(i === current));
      });
      counter.textContent = String(current + 1).padStart(2, "0");
    };

    const stop = () => {
      clearInterval(timer);
      timer = null;
    };
    const start = () => {
      stop();
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      timer = setInterval(() => goTo(current + 1), CONFIG.autoplayMs);
    };
    const restart = () => start();

    $$("[data-hero-next]", slider).forEach((btn) =>
      btn.addEventListener("click", () => { goTo(current + 1); restart(); })
    );
    $$("[data-hero-prev]", slider).forEach((btn) =>
      btn.addEventListener("click", () => { goTo(current - 1); restart(); })
    );
    tabs.forEach((tab, i) => tab.addEventListener("click", () => { goTo(i); restart(); }));

    slider.addEventListener("focusin", stop);
    slider.addEventListener("focusout", (e) => {
      if (!slider.contains(e.relatedTarget)) start();
    });
    document.addEventListener("visibilitychange", () =>
      document.hidden ? stop() : start()
    );

    let touchX = null;
    slider.addEventListener("touchstart", (e) => {
      touchX = e.touches[0].clientX;
      stop();
    }, { passive: true });
    slider.addEventListener("touchend", (e) => {
      if (touchX !== null) {
        const dx = e.changedTouches[0].clientX - touchX;
        if (Math.abs(dx) > CONFIG.swipePx) goTo(current + (dx < 0 ? 1 : -1));
      }
      touchX = null;
      start();
    }, { passive: true });

    goTo(0);
    start();
  }

  /* ---------- 15. Постепенная загрузка ----------
     Первый экран грузится как обычно. Когда окно готово, в простое
     по очереди доезжают слайды 2–5, затем ленивые картинки прогревают
     кеш — к скроллу они уже на месте. */
  const whenIdle = (cb) => {
    if ("requestIdleCallback" in window) requestIdleCallback(cb, { timeout: 2000 });
    else setTimeout(cb, 300);
  };

  const loadDeferredSlides = (done) => {
    const deferred = $$(".hero__slide img[data-src]");
    const step = (i) => {
      if (i >= deferred.length) { done(); return; }
      const img = deferred[i];
      const next = () => step(i + 1);
      img.addEventListener("load", next, { once: true });
      img.addEventListener("error", next, { once: true });
      img.src = img.dataset.src;
      img.removeAttribute("data-src");
    };
    step(0);
  };

  const warmLazyImages = () => {
    const urls = $$("img[loading='lazy']").map((img) => img.src);
    const step = (i) => {
      if (i >= urls.length) return;
      const probe = new Image();
      probe.onload = probe.onerror = () => step(i + 1);
      probe.src = urls[i];
    };
    step(0);
  };

  window.addEventListener("load", () =>
    whenIdle(() => loadDeferredSlides(() => whenIdle(warmLazyImages)))
  );
})();
