import {controlFetch} from './control-session.js';
/** Page connection settings share the original SSH channels and sudo credential. */
import {formatError} from './messages.js';
export class SSHConnection {
  constructor(cfg,beforeChange,changed){
    this.cfg=cfg;this.beforeChange=beforeChange;this.changed=changed;this.working=false;
    this.$=id=>document.getElementById(id);
    this.$('ssh-target').value=cfg.ssh_target||'';
    this.$('ssh-target').disabled=!cfg.connection_ui;
    this.$('ssh-form').onsubmit=e=>{e.preventDefault();this.submit('connect');};
    this.$('ssh-disconnect').onclick=()=>this.submit('disconnect');
    this.paint();
  }
  async submit(action){
    if(this.working||!this.cfg.connection_ui)return;
    this.working=true;this.paint();this.beforeChange();
    let responseData=false;
    try{
      const response=await controlFetch('/api/connection',{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':this.cfg.control_token},
        body:JSON.stringify({action,target:this.$('ssh-target').value,password:this.$('service-sudo-password').value})});
      const data=await response.json();responseData=true;if(!response.ok||!data.accepted){const error=Error(data.error||'连接设置未完成');error.local=response.status===403||response.status===409;throw error;}
      this.cfg.ssh_target=data.target;this.cfg.camera_host=data.camera_host;
      this.$('ssh-target').value=data.target;this.state=data;this.changed(data);
      const local=this.$('local-connection-banner');if(local?.dataset.source==='request')local.hidden=true;
      this.$('ssh-status').textContent=action==='connect'?'正在建立 SSH 会话…':'SSH 连接已暂停';
    }catch(e){
      if(!responseData||e.local||e.sessionChanged){const local=this.$('local-connection-banner');if(local){local.hidden=false;local.dataset.source='request';local.className='banner warning';local.textContent='本地中控请求未完成：'+formatError(e);}this.$('ssh-status').textContent='SSH 请求状态尚未确认';}
      else this.$('ssh-status').textContent=formatError(e);
    }
    finally{this.working=false;this.paint();}
  }
  render(snapshot,{offline=false,replay=false}={}){
    if(replay)return;
    if(offline){this.$('ssh-status').textContent='SSH 状态待确认';return;}
    if(!snapshot.connection||this.working)return;
    this.state=snapshot.connection;
    const endpointError=Object.values(snapshot.channels||{}).find(channel=>channel.status==='error'&&channel.error_source==='ssh')?.error;
    this.$('ssh-status').textContent=this.state.connected?'SSH 已连接'+(endpointError?'；SSH 通道异常：'+formatError(endpointError):''):this.state.active?formatError(this.state.error||endpointError)||'正在建立 SSH 会话…':'未连接，填写地址和密码后连接';
    this.paint();
  }
  paint(){
    this.$('ssh-connect').disabled=this.working||!this.cfg.connection_ui;
    this.$('ssh-connect').textContent=this.working?'连接中…':this.state?.active?'重连':'连接';
    this.$('ssh-disconnect').disabled=this.working||!this.state?.active;
  }
}
