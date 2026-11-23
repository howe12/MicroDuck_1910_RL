/** Keep the original session log; compact only standalone JSON data blocks. */
import {formatError} from './messages.js';
const actions={capture:'关节标定',move:'关节移动','switch-mode':'模式切换',init:'回 HOME',relax:'停止并松开','verify-imu':'IMU 安装标定','set-pad-settings':'手柄设置','set-action-scale':'动作缩放','chorale-prepare':'准备合唱','chorale-start':'开始合唱','chorale-stop':'停止合唱','reconnect-service':'重启固件','upgrade-service':'升级固件',reboot:'重启 Zero',shutdown:'关闭 Zero','enable-hotspot':'开启热点'};
const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const reason=value=>typeof value==='string'?value:object(value)?[value.message,value.reason,typeof value.error==='string'?value.error:''].filter(x=>typeof x==='string'&&x).join('；'):'';
function jsonSummary(value){
  if(Array.isArray(value))return `已接收 ${value.length} 条数据。`;
  if(object(value.command)&&value.command.action){
    const command=value.command,label=actions[command.action]||command.action;
    return label+(Number.isInteger(command.id)?` ID ${command.id}`:'')+'：执行中';
  }
  if(typeof value.result==='string')return value.result;
  if(Array.isArray(value.result))return `已接收 ${value.result.length} 条数据。`;
  const data=object(value.result)?value.result:value;
  const error=reason(value.error)||reason(data.error);
  if(error)return '操作失败：'+error;
  if(data.accepted===false||data.success===false||data.ok===false||['error','failed'].includes(data.status))return '操作未完成'+(reason(data)?'：'+reason(data):'。');
  if(data.saved===true)return '保存成功'+(Number.isInteger(data.id)?` · ID ${data.id}`:'')+(reason(data)?'；'+reason(data):'。');
  if(reason(data))return reason(data);
  if(data.scheduled===true)return '操作已提交；等待实际状态确认。';
  if(data.accepted===true)return '请求已接收。';
  if(data.success===true||data.ok===true)return '操作成功。';
  if(typeof data.mode==='string')return '当前模式：'+({commissioning:'标定模式',motion:'运动模式',numbering:'编号模式',hardware_test:'测试模式'}[data.mode]||data.mode);
  return '数据回执已收到。';
}
function jsonEnd(text,start){
  const stack=[];let quoted=false,escaped=false;
  for(let i=start;i<text.length;i++){
    const char=text[i];
    if(quoted){if(escaped)escaped=false;else if(char==='\\')escaped=true;else if(char==='"')quoted=false;continue;}
    if(char==='"'){quoted=true;continue;}
    if(char==='{'||char==='[')stack.push(char);
    else if(char==='}'||char===']'){
      if(stack.pop()!==(char==='}'?'{':'['))return -1;
      if(!stack.length)return i+1;
    }
  }
  return -1;
}
function compactJson(text){
  const parts=[];let copied=0,start=0;
  while(start<text.length){
    let first=start;while(text[first]===' '||text[first]==='\t')first++;
    if(text[first]==='{'||text[first]==='['){
      const end=jsonEnd(text,first);
      if(end!==-1){
        try{
          const value=JSON.parse(text.slice(first,end));
          const lineEnd=text.indexOf('\n',end);
          if(text.slice(end,lineEnd===-1?text.length:lineEnd).trim())throw Error('Not a standalone JSON block');
          parts.push(text.slice(copied,first),jsonSummary(value));copied=end;
          const next=text.indexOf('\n',end);if(next===-1)break;start=next+1;continue;
        }catch{} // Text with brackets, Python dictionaries and malformed JSON stays visible.
      }else if(text.includes('[过长单条输出：',first)&&/^\{\s*"[^"]+"\s*:/.test(text.slice(first))){
        parts.push(text.slice(copied,first),'JSON 数据报文过长，首尾内容保留在下载日志中。');copied=text.length;break;
      }
    }
    const next=text.indexOf('\n',start);if(next===-1)break;start=next+1;
  }
  parts.push(text.slice(copied));return parts.join('');
}
export function operationLogEntry(row){
  const source=String(row.source??''),text=compactJson(String(row.text??''));
  // Restore the existing Chinese explanation plus original error behavior.
  return {source,text:/错误|异常|失败|stderr/i.test(source)?formatError(text):text};
}
const line=(at,source,text)=>`[${new Date(at*1000).toLocaleTimeString('zh-CN',{hour12:false})}] [${source}] ${String(text).replace(/(?:\r?\n)+$/,'')}`;
export class SystemLogPanel {
  constructor(){
    this.cursor=0;this.records=[];this.lines=[];this.stopped=false;this.saveError='';
    this.root=document.getElementById('system-log');this.pre=document.getElementById('system-log-output');
    document.getElementById('system-log-follow').checked=true;
    document.getElementById('system-log-copy').onclick=async()=>{
      try{await navigator.clipboard.writeText(this.pre.textContent);this.status('已复制日志。');}
      catch{this.status('可选中日志文字复制，或下载日志。');}
    };
    window.addEventListener('pagehide',()=>{this.stopped=true;clearTimeout(this.timer);});
    this.poll();
  }
  status(text){document.getElementById('system-log-status').textContent=text;}
  append(row){
    const displayed=operationLogEntry(row);
    this.records.push(line(row.at,displayed.source,displayed.text));
  }
  bound(){
    if(this.records.length>1500)this.records.splice(0,this.records.length-1500);
    let chars=this.records.reduce((sum,row)=>sum+row.length+1,0);
    while(chars>256*1024&&this.records.length>1)chars-=this.records.shift().length+1;
    if(this.records[0]?.length>256*1024){const note='··· 本行页面只显示末尾，原始记录请下载日志 ···\n';this.records[0]=note+this.records[0].slice(-(256*1024-note.length));}
  }
  render(){
    this.lines=this.records;
    this.pre.textContent=this.lines.join('\n')||'尚无执行记录。';
    if(document.getElementById('system-log-follow').checked)this.pre.scrollTop=this.pre.scrollHeight;
    this.status(this.saveError?'系统日志保存异常：'+formatError(this.saveError):'保留启动、连接、操作与异常；仅精简 JSON 数据报文。');
  }
  async poll(){
    let again=false;
    try{
      if(document.hidden)return;
      const response=await fetch('/api/system-log?after='+this.cursor+'&limit=200',{signal:AbortSignal.timeout(5000)});
      if(!response.ok)throw Error('HTTP '+response.status);
      const data=await response.json();
      if(!Array.isArray(data.entries))throw Error('日志响应无效');
      if(data.gap)this.records.push('··· 页面仅显示最近记录；下载日志查看之前的记录 ···');
      for(const row of data.entries)this.append(row);
      this.cursor=data.cursor;this.saveError=data.error||'';this.bound();this.render();
      again=data.has_more;
    }catch(e){this.status('系统日志读取中断：'+formatError(e)+'；稍后重新读取。');}
    finally{if(!this.stopped)this.timer=setTimeout(()=>this.poll(),again?50:1000);}
  }
}
