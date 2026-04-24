const tenantSelect = document.getElementById('tenant');
const tenantDescription = document.getElementById('tenant-description');
const tokenInput = document.getElementById('tenant-token');
const questionInput = document.getElementById('question');
const sendButton = document.getElementById('send');
const answerBox = document.getElementById('answer');
const statusLabel = document.getElementById('status');
const metaBox = document.getElementById('meta');

let tenants = [];

async function loadTenants() {
  const response = await fetch('/api/tenants');
  tenants = await response.json();
  tenantSelect.innerHTML = '';

  tenants.forEach((tenant) => {
    const option = document.createElement('option');
    option.value = tenant.tenant_id;
    option.textContent = `${tenant.display_name} (${tenant.namespace})`;
    tenantSelect.appendChild(option);
  });

  refreshTenantDescription();
}

function refreshTenantDescription() {
  const current = tenants.find((item) => item.tenant_id === tenantSelect.value);
  tenantDescription.textContent = current?.description || '';
}

async function sendQuestion() {
  const tenantId = tenantSelect.value;
  const token = tokenInput.value.trim();
  const question = questionInput.value.trim();

  if (!tenantId || !token || !question) {
    statusLabel.textContent = '请先选择租户并填写令牌、问题';
    return;
  }

  statusLabel.textContent = '请求中…';
  answerBox.textContent = '正在调用推理服务，请稍候…';
  metaBox.textContent = '';
  sendButton.disabled = true;

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Tenant-ID': tenantId,
        'X-Tenant-Token': token,
      },
      body: JSON.stringify({ question, history: [] }),
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || '请求失败');
    }

    answerBox.textContent = data.answer || '模型未返回内容';
    statusLabel.textContent = '请求完成';
    metaBox.textContent = `tenant=${data.tenant_id} | model=${data.model_id} | ray=${data.ray_service}`;
  } catch (error) {
    answerBox.textContent = error.message;
    statusLabel.textContent = '请求失败';
  } finally {
    sendButton.disabled = false;
  }
}

tenantSelect.addEventListener('change', refreshTenantDescription);
sendButton.addEventListener('click', sendQuestion);
loadTenants().catch((error) => {
  statusLabel.textContent = '初始化失败';
  answerBox.textContent = error.message;
});
