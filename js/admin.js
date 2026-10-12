document.querySelectorAll('[data-copy-link]').forEach((button) => {
  button.addEventListener('click', async () => {
    const link = document.getElementById(button.dataset.copyLink);
    const status = document.getElementById('max-copy-status');
    const field = document.getElementById('max-invite-url');
    if (!link || !status || !field) return;
    button.disabled = true;
    try {
      try {
        await navigator.clipboard.writeText(link.href);
      } catch {
        field.hidden = false;
        field.focus();
        field.select();
        if (!document.execCommand('copy')) throw new Error('copy unavailable');
      }
      field.hidden = true;
      status.textContent = 'Ссылка скопирована. Отправьте её нужному человеку — она действует 10 минут с момента создания.';
      button.textContent = 'Скопировано';
      button.focus();
    } catch {
      field.hidden = false;
      field.focus();
      field.select();
      status.textContent = 'Не удалось скопировать автоматически. Скопируйте выделенную ссылку вручную.';
    } finally {
      button.disabled = false;
    }
  });
});
