const $ = id => document.getElementById(id);
let turnId = null, running = false, realVoice = null, state = null, requested = false, boundaryReached = false;
const setText = (id, value) => { $(id).textContent = value; };
const post = async (path, body = {}) => { const r = await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const data=await r.json(); if(!r.ok) throw Error(data.error || 'Request failed'); return data; };
function step(name, style, value) { const el=$(`step-${name}`); el.classList.remove('good','bad','warn'); if(style) el.classList.add(style); setText(`${name}-state`,value); }
function factor(name,status,detail) { const good=status==='MATCH'; const bad=status==='MISMATCH'; step(name,good?'good':bad?'bad':'warn',status); setText(`${name}-detail`,detail); }
function bubble(text, cls='') { const el=document.createElement('div'); el.className=`bubble ${cls}`; el.textContent=text; $('chat').append(el); $('chat').scrollTop=$('chat').scrollHeight; }
function device(dot,status,on,text) { $(dot).classList.toggle('on',on); setText(status,text); }
function render(data) {
 state=data; const v=data.hardware.vexo, p=data.polarVerifier;
 device('vexo-dot','vexo-status',v.status==='connected',v.status==='connected'?(v.microphoneOn?'Microphone active':'Connected'):v.status==='unknown'?'Connecting…':'Disconnected');
 device('polar-dot','polar-status',!!p.device?.streaming,p.device?.streaming?`PPG live · ${p.device.ppgSamples ?? 0} samples`:p.device?.connected?'Connected':'Disconnected');
 device('gemini-dot','gemini-status',data.agent.configured,data.agent.configured?'Configured':'Unavailable');
 device('stripe-dot','stripe-status',data.stripe.configured,data.stripe.configured?'Configured':'Unavailable');
 $('connect-vexo').hidden=v.status==='connected'; $('connect-vexo').disabled=v.status==='unknown';
 $('connect-polar').hidden=!!p.running; $('connect-polar').disabled=data.identityMode!=='biometric';
 $('listen').disabled=running||v.status!=='connected';
 if (!running && v.status!=='connected') setText('listen-status','Connect Vexo to begin. Audio is transcribed locally.');
 const voice=data.biometrics.voiceSpeaker, heart=data.biometrics.polarPpg, actual=p.latestVerification;
 const history=actual?.history || [], window=history.at(-1), quality=actual?.quality || window?.quality;
 const format=value=>typeof value==='number'&&Number.isFinite(value)?value.toFixed(3):'—';
 setText('details',`Voice: ${voice.status} · similarity ${format(voice.similarity)} · ${voice.expiresAt?.startsWith('9999') ? 'valid until Vexo disconnects or a new verification' : `expires ${voice.expiresAt || '—'}`}\nHeartbeat authorization: ${heart.status} · ${actual?.reason || 'waiting for verification'}\nEXPERIMENTAL heartbeat matcher: fitted to one enrolled wearer and one other wearer; not independently validated. Similarity threshold: ${format(actual?.threshold)}.\nPulse-PPG similarity: ${format(actual?.similarity)} · pulse morphology distance: ${format(actual?.morphologyDistance)}\nAPG morphology distance: ${format(window?.apgDistance)} · estimated heart rate: ${quality?.heart_rate_bpm_estimate != null ? Math.round(quality.heart_rate_bpm_estimate)+' bpm' : '—'}\nSignal quality: ${window ? (window.quality_approved ? 'passed' : `rejected (${(window.rejection_reasons || []).join(', ')})`) : 'waiting'}\nPolar stream: ${p.device?.streaming ? 'receiving PPG and accelerometer' : 'unavailable'}\nAPG and heart rate are displayed only; heart rate alone does not identify a person.`);
 if(!turnId) factor('voice',voice.status,`Real voice verifier: ${voice.status}`);
 factor('heart',heart.status,`Live PPG verifier: ${heart.status}${heart.status==='UNCERTAIN' && actual?.reason==='top_k_threshold_requires_calibration' ? ' · threshold not calibrated' : ''}`);
}
async function refresh(){const r=await fetch('/api/state');if(!r.ok)throw Error('Server unavailable');render(await r.json());}
$('connect-vexo').onclick=async()=>{try{await post('/api/hardware/vexo/connect');await refresh();}catch(e){setText('listen-status',e.message)}};
$('connect-polar').onclick=async()=>{try{await post('/api/hardware/polar/start');await refresh()}catch(e){setText('listen-status',e.message)}};
$('listen').onclick=async()=>{
 if(running)return; running=true;turnId=null;realVoice=null;requested=false;boundaryReached=false; $('listen').disabled=true;
 ['request','voice','heart','decision'].forEach(x=>step(x,'','—'));
 setText('request-detail','Waiting for a request');setText('voice-detail','Waiting for voice verification');setText('heart-detail','Waiting for heartbeat verification');setText('decision-detail','Waiting for authorization check');setText('result-title','No Stripe request yet');setText('result-detail','The outcome will appear here.');setText('listen-status','Preparing the Vexo microphone…');
 try {const r=await post('/api/demo/listen');turnId=r.turnId;setText('listen-status','Listening on Vexo… Speak your request.');}
 catch(e){running=false;setText('listen-status',e.message);await refresh();}
};
const stream=new EventSource('/api/events');
stream.addEventListener('clasp',async ev=>{
 const e=JSON.parse(ev.data),d=e.data||{};
 if(e.type==='hardware.vexo.connection'&&d.status==='disconnected'&&running){running=false;setText('listen-status','Vexo disconnected. Reconnect and try again.');}
 if(e.type==='voice.capture.failed'&&d.turnId===turnId){running=false;setText('listen-status',d.reason||'Vexo capture failed.');}
 if(e.type==='identity.voice_verification'&&d.turnId===turnId){realVoice=d;factor('voice',d.decision,`Real voice verifier: ${d.decision}`);}
 if(e.type==='voice.capture.completed'&&d.turnId===turnId)setText('listen-status','Speech captured · verifying and transcribing locally…');
 if(e.type==='voice.transcription'&&d.turnId===turnId){
  if(!d.text?.trim()){running=false;setText('listen-status',d.reason||'No speech recognized. Try again.');await refresh().catch(()=>{});return;}
  bubble(d.text,'user');setText('listen-status','Speech recognized · Gemini is reasoning…');
  try{const result=await post('/api/demo/run',{turnId});bubble(result.answer);setText('listen-status',`Gemini response · ${result.model}`);if(!requested){step('request','warn','NO TOOL');setText('request-detail','Gemini did not request a payment tool.');}}
  catch(err){bubble(err.message);setText('listen-status',err.message);}
  finally{running=false;await refresh().catch(()=>{});}
 }
 if(e.type==='agent.tool.requested'&&d.requestId===turnId){requested=true;step('request','good','REQUESTED');setText('request-detail',`Stripe TEST · $${Number(d.requestedAction?.amountUsd||0).toFixed(2)} · demo merchant`);setText('listen-status','Gemini requested a tool · Clasp is checking…');}
 if(e.type==='demo.boundary'&&d.requestId===turnId){boundaryReached=true;const v=d.biometrics?.voiceSpeaker?.status||'UNAVAILABLE',h=d.biometrics?.polarPpg?.status||'UNAVAILABLE';factor('voice',v,`At authorization: ${v}`);factor('heart',h,`At authorization: ${h}`);step('decision','warn','CHECKING');setText('decision-detail','Evaluating fresh biometric evidence and payment policy…');}
 if(e.type==='authorization.decision'&&d.requestId===turnId){step('decision',d.allowed?'good':'bad',d.allowed?'AUTHORIZED':'BLOCKED');setText('decision-detail',d.reason);}
 if(e.type==='tool.execution.completed'&&d.requestId===turnId){const pi=d.paymentIntent;if(pi){setText('result-title',`Stripe TEST · ${String(pi.status).toUpperCase()}`);setText('result-detail',`PaymentIntent ${pi.id} · $${(pi.amount/100).toFixed(2)} ${pi.currency.toUpperCase()} · livemode false`);}else{setText('result-title',d.stripeCalled?'Stripe TEST returned an error':'Stripe was not called');setText('result-detail',d.reason||'No external request was made.');}}
 if(e.type==='hardware.vexo.audio'&&running)setText('listen-status',`Listening on Vexo · ${d.packets} audio packets received`);
 if(e.type==='hardware.vexo.connection'||e.type==='hardware.vexo.microphone'||e.type==='hardware.polar.worker'||e.type==='identity.biometric_factors')refresh().catch(()=>{});
});
stream.onerror=()=>setText('listen-status','Live connection interrupted. Reconnecting…');
setInterval(()=>refresh().catch(()=>{}),4000);refresh().catch(()=>setText('listen-status','Could not reach Clasp.'));
