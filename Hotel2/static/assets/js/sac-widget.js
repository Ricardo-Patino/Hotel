(function(){
  const el = (id)=>document.getElementById(id);
  const fab = el("sac-fab"), panel=el("sac-panel"), close=el("sac-close");
  const chat = el("sac-chat"), input=el("sac-input"), send=el("sac-send");
  const btnEsc = el("sac-escalar"), wa = el("sac-whatsapp"), tel = el("sac-llamar");
  if(!fab || !panel) return;

  const isMobile = /Android|iPhone|iPad/i.test(navigator.userAgent);
  const phone    = "+50626420225";
  const waBase   = isMobile ? "https://wa.me/" : "https://web.whatsapp.com/send?phone=";
  wa.href = waBase + phone;
  tel.href = (isMobile ? "tel:" : "callto:") + phone;

  function toggle(){ panel.classList.toggle("d-none"); }
  fab.addEventListener("click", toggle);
  el("sac-close").addEventListener("click", ()=>panel.classList.add("d-none"));

  function append(role, text){
    const div = document.createElement("div");
    div.className = "sac-msg sac-"+role;
    div.textContent = text;
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
  }
  function ask(){
    const q = (input.value || "").trim();
    if(!q) return;
    append("user", q); input.value="";
    fetch("/sac/chat/ask",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({q})
    }).then(r=>r.json()).then(d=>append("bot", d.a || "…"))
      .catch(()=>append("bot","Ha ocurrido un error."));
  }
  send.addEventListener("click", ask);
  input.addEventListener("keydown",(e)=>{ if(e.key==="Enter") ask(); });

  btnEsc.addEventListener("click", ()=>{
    const last = Array.from(chat.querySelectorAll(".sac-msg.sac-user")).pop();
    const texto = last ? last.textContent : "Cliente solicita agente";
    fetch("/sac/chat/escalar",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({texto})
    }).then(()=>append("bot","Listo, un agente te contactará pronto."));
  });
})();
