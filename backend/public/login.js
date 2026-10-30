document.querySelector('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target, message = document.querySelector('#message'), button = form.querySelector('button');
  button.disabled = true; message.textContent = '';
  try {
    const response = await fetch('/api/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(Object.fromEntries(new FormData(form))) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Sign-in failed');
    location.assign('/');
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
});
