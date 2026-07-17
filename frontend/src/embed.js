/**
 * embed.js — SDK entry for host systems to embed the form modeler.
 *
 * Usage in host system:
 *   <script src="https://modeler.example.com/embed.js"></script>
 *   <script>
 *     const modeler = new LLMFormModeler({
 *       baseUrl: 'https://modeler.example.com',
 *       userId: 'user_123',
 *       onConfigGenerated: (config) => { ... },
 *       onConfigApply: (config) => { ... },
 *     })
 *     modeler.open()
 *   </script>
 *
 * This file is built separately (Vite multi-entry) as a standalone JS file.
 */
export class LLMFormModeler {
    options;
    container = null;
    iframe = null;
    floatBtn = null;
    isOpen = false;
    constructor(options) {
        this.options = {
            position: 'bottom-right',
            theme: 'light',
            ...options,
        };
        this.init();
    }
    init() {
        // Create floating button
        this.floatBtn = document.createElement('div');
        this.floatBtn.style.cssText = `
      position: fixed;
      ${this.options.position === 'bottom-left' ? 'left' : 'right'}: 24px;
      bottom: 24px;
      width: 56px; height: 56px;
      border-radius: 50%;
      background: #1677ff;
      color: #fff;
      display: flex; align-items: center; justify-content: center;
      font-size: 24px; cursor: pointer;
      box-shadow: 0 4px 12px rgba(22,119,255,0.4);
      z-index: 99999;
      transition: transform 0.2s;
    `;
        this.floatBtn.innerHTML = '💬';
        this.floatBtn.onmouseenter = () => (this.floatBtn.style.transform = 'scale(1.1)');
        this.floatBtn.onmouseleave = () => (this.floatBtn.style.transform = 'scale(1)');
        this.floatBtn.onclick = () => this.toggle();
        document.body.appendChild(this.floatBtn);
        // Listen for messages from iframe
        window.addEventListener('message', (e) => this.handleMessage(e));
    }
    handleMessage(e) {
        const { type, payload } = e.data || {};
        switch (type) {
            case 'MODELER_CONFIG_GENERATED':
                this.options.onConfigGenerated?.(payload.config);
                break;
            case 'MODELER_CONFIG_APPLY':
                this.options.onConfigApply?.(payload.config);
                break;
            case 'MODELER_CLOSE':
                this.close();
                break;
        }
    }
    createIframe() {
        this.container = document.createElement('div');
        this.container.style.cssText = `
      position: fixed;
      ${this.options.position === 'bottom-left' ? 'left' : 'right'}: 24px;
      bottom: 90px;
      width: 400px; height: 600px;
      max-width: calc(100vw - 48px); max-height: calc(100vh - 120px);
      border-radius: 12px;
      box-shadow: 0 8px 32px rgba(0,0,0,0.15);
      overflow: hidden;
      z-index: 99999;
      background: #fff;
    `;
        this.iframe = document.createElement('iframe');
        const params = new URLSearchParams({
            embed: 'true',
            ...(this.options.userId ? { userId: this.options.userId } : {}),
        });
        this.iframe.src = `${this.options.baseUrl}/?${params}`;
        this.iframe.style.cssText = 'width: 100%; height: 100%; border: none;';
        this.iframe.allow = 'clipboard-write';
        this.container.appendChild(this.iframe);
        document.body.appendChild(this.container);
    }
    open() {
        if (this.isOpen)
            return;
        this.createIframe();
        this.isOpen = true;
    }
    close() {
        if (!this.isOpen)
            return;
        this.container?.remove();
        this.container = null;
        this.iframe = null;
        this.isOpen = false;
        this.options.onClose?.();
    }
    toggle() {
        this.isOpen ? this.close() : this.open();
    }
    destroy() {
        this.close();
        this.floatBtn?.remove();
        this.floatBtn = null;
        window.removeEventListener('message', this.handleMessage);
    }
    /** Set context (e.g., current form being edited) */
    setContext(context) {
        if (context.userId)
            this.options.userId = context.userId;
        if (this.iframe) {
            this.iframe.contentWindow?.postMessage({ type: 'MODELER_INIT', payload: context }, '*');
        }
    }
}
// Auto-register on window
if (typeof window !== 'undefined') {
    ;
    window.LLMFormModeler = LLMFormModeler;
}
export default LLMFormModeler;
//# sourceMappingURL=embed.js.map