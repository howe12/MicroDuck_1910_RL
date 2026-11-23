/** Per-backend credentials. Refresh reads context only; control requests are never replayed. */
let context=null,refreshing=null;

export class ControlSessionChangedError extends Error {
  constructor(message,{status=403,uncertain=false}={}){
    super(message);this.name='ControlSessionChangedError';this.status=status;
    this.code='control_session_expired';this.sessionChanged=true;this.retryRequired=true;
    this.accepted=false;this.uncertain=uncertain;
  }
}

export function initControlSession(config){
  if(!config||typeof config!=='object'||typeof config.control_token!=='string')throw Error('中控会话配置无效');
  if(!context){context=config;return context;}
  const previous=context.control_session_id,changed=context.control_token!==config.control_token||previous!==config.control_session_id;
  Object.assign(context,config);
  if(changed&&typeof window!=='undefined')window.dispatchEvent(new CustomEvent('r17-control-session-changed',{
    detail:{previous,current:context.control_session_id,message:'中控会话已更新；旧操作未自动重发，请重新点击。'}
  }));
  return context;
}

export function getControlToken(fallback){return context?.control_token??fallback?.control_token??'';}

export async function refreshControlSession(){
  if(refreshing)return refreshing;
  refreshing=(async()=>{
    const response=await fetch('/api/config',{method:'GET',cache:'no-store',credentials:'same-origin',mode:'same-origin',signal:AbortSignal.timeout(4000)});
    const config=await response.json();if(!response.ok)throw Error(config.error||'中控会话读取失败');
    return initControlSession(config);
  })();
  try{return await refreshing;}finally{refreshing=null;}
}

export async function controlFetch(path,options={}){
  const headers=new Headers(options.headers),token=getControlToken();
  if(token)headers.set('X-Control-Token',token);
  const sentToken=headers.get('X-Control-Token');
  // Exactly one POST, including uploads and keepalive cleanup. A lost receipt,
  // rejected token, or conflict must never replay motion, stop, or numbered writes.
  const response=await fetch(path,{...options,headers,credentials:'same-origin',mode:'same-origin',cache:'no-store'});
  if(response.status===403){
    let failure;try{failure=await response.clone().json();}catch{}
    if(failure?.code==='control_session_expired'&&failure.action_executed===false){
      let refreshed=false;try{await refreshControlSession();refreshed=true;}catch{}
      throw new ControlSessionChangedError(refreshed&&getControlToken()!==sentToken
        ?'中控已重新启动，页面凭据已更新；本次操作未执行，请重新点击。'
        :'页面凭据校验未通过；本次操作未执行，请刷新页面后重新点击。');
    }
  }
  if(response.status===409){try{await refreshControlSession();}catch{}}
  if(sentToken&&getControlToken()&&sentToken!==getControlToken())throw new ControlSessionChangedError(
    '中控会话已变化，旧操作未自动重发；请核对当前状态后重新操作。',{status:409,uncertain:response.ok});
  return response;
}
