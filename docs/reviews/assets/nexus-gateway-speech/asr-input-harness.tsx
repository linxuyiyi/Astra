import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import i18n from 'i18next';
import {initReactI18next} from 'react-i18next';
import zh from '../../../../services/nexus-api/chat-island/src/i18n/locales/zh.json';
import {TextInput} from '../../../../services/nexus-api/chat-island/src/components/chat/TextInput.tsx';
import {TooltipProvider} from '../../../../services/nexus-api/chat-island/src/components/ui/tooltip.tsx';
await i18n.use(initReactI18next).init({lng:'zh',resources:{zh:{translation:zh}}});
const fixture = await fetch('/fixture.wav').then(r=>r.blob());
let failure=false, microphoneStarts=0;
const originalFetch=window.fetch.bind(window);
window.fetch=((input,options)=>failure && input==='/api/asr/transcribe' ? Promise.resolve(new Response(JSON.stringify({detail:'测试识别故障，请重试'}),{status:503,headers:{'Content-Type':'application/json'}})):originalFetch(input,options)) as typeof fetch;
Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia:async()=>{microphoneStarts++;return {getTracks:()=>[{stop(){}}]}}},configurable:true});
class FakeRecorder {
 static isTypeSupported(){return false}
 state='inactive';mimeType='audio/wav';ondataavailable:any;onstop:any;onerror:any;
 start(){this.state='recording'}
 stop(){this.state='inactive';this.ondataavailable?.({data:fixture});this.onstop?.()}
}
(window as any).MediaRecorder=FakeRecorder;
function App(){const [sends,setSends]=useState(0);const [fail,setFail]=useState(false);return <main style={{maxWidth:900,margin:'40px auto',padding:24}}><h1>真实 TextInput / useAsrRecorder 验收</h1><p>录音设备替身输入测试 WAV；成功识别经过真实 Nexus 服务、网关与 ASR。硬件麦克风未测试。</p><label><input type="checkbox" checked={fail} onChange={e=>{failure=e.target.checked;setFail(failure)}}/>模拟识别失败</label><p>自动发送次数：{sends}</p><TextInput fileProcessor={async()=>null} onSend={()=>setSends(x=>x+1)} placeholder="输入原有草稿"/><p>私聊和群聊分别用 URL conversation/channel 参数隔离草稿。</p></main>}
createRoot(document.getElementById('root')!).render(<TooltipProvider><App/></TooltipProvider>);
