document.querySelector('[data-product-add]')?.addEventListener('click', (event) => {
  const button = event.currentTarget;
  const name = button.dataset.name;
  const price = button.dataset.price;
  let cart = [];
  try {
    const saved = JSON.parse(localStorage.getItem('gd-cart') || '[]');
    if (Array.isArray(saved)) cart = saved.filter(item => item && typeof item.name === 'string');
  } catch { /* A damaged cart starts over. */ }
  const existing = cart.find(item => item.name === name);
  if (existing) existing.quantity = Math.min(999, (Number(existing.quantity) || 0) + 1);
  else cart.push({name, price, quantity: 1});
  try { localStorage.setItem('gd-cart', JSON.stringify(cart)); } catch { /* Private mode may block storage. */ }
  window.location.assign('/catalog');
});
