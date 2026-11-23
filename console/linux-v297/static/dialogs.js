// Shared page dialogs. Text is never interpreted as markup or a hardware command.
let active=null,serial=0,clearing=false;
const queue=[],keys=new Map();

function finish(job,answer){
  if(job.done)return;
  job.done=true;job.options.signal?.removeEventListener('abort',job.abort);
  const index=queue.indexOf(job);if(index>=0)queue.splice(index,1);
  if(job.options.key&&keys.get(job.options.key)===job.promise)keys.delete(job.options.key);
  if(active===job){
    active=null;if(job.dialog?.open)job.dialog.close();job.dialog?.remove();
    if(job.focus?.isConnected)job.focus.focus?.({preventScroll:true});
  }
  job.resolve(answer);if(!clearing)pump();
}

function pump(){
  if(active||clearing||!queue.length)return;
  const job=active=queue.shift(),o=job.options,id='page-dialog-'+(++serial);
  job.focus=document.activeElement;
  const dialog=job.dialog=document.createElement('dialog');dialog.className='page-message-dialog';
  dialog.dataset.tone=o.tone||'info';dialog.setAttribute('role',o.tone==='error'?'alertdialog':'dialog');
  dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-labelledby',id+'-title');dialog.setAttribute('aria-describedby',id+'-copy');
  const title=document.createElement('h2');title.id=id+'-title';title.textContent=o.title;
  const copy=document.createElement('div');copy.id=id+'-copy';copy.className='page-dialog-copy';copy.textContent=job.message;
  const actions=document.createElement('div');actions.className='page-dialog-actions';
  let cancel;
  if(job.kind==='confirm'){
    cancel=document.createElement('button');cancel.type='button';cancel.className='button';cancel.textContent=o.cancelText;cancel.autofocus=true;
    cancel.addEventListener('click',()=>finish(job,false));actions.append(cancel);
  }
  const accept=document.createElement('button');accept.type='button';accept.className='button accent';accept.textContent=o.confirmText;accept.autofocus=!cancel;
  accept.addEventListener('click',()=>finish(job,true));actions.append(accept);dialog.append(title,copy,actions);
  dialog.addEventListener('cancel',event=>{event.preventDefault();finish(job,false);});
  dialog.addEventListener('close',()=>finish(job,false));document.body.append(dialog);
  try{dialog.showModal();(cancel||accept).focus();}catch{finish(job,false);}
}

function request(kind,message,options){
  if(options.signal?.aborted)return Promise.resolve(false);
  if(options.key&&keys.has(options.key))return keys.get(options.key);
  const job={kind,message:String(message?.message??message??''),options,done:false};
  job.promise=new Promise(resolve=>{job.resolve=resolve;});job.abort=()=>finish(job,false);
  if(options.key)keys.set(options.key,job.promise);
  options.signal?.addEventListener('abort',job.abort,{once:true});queue.push(job);pump();return job.promise;
}

export function showMessageDialog(message,{title='提示',confirmText='知道了',tone='info',key,signal}={}){
  return request('message',message,{title,confirmText,tone,key,signal});
}

export function confirmDialog(message,{title='确认操作',confirmText='确定',cancelText='取消',tone='info',key,signal}={}){
  return request('confirm',message,{title,confirmText,cancelText,tone,key,signal});
}

function cancelAll(){
  clearing=true;for(const job of [...queue])finish(job,false);if(active)finish(active,false);clearing=false;
}
if(typeof window!=='undefined')for(const event of ['pagehide','r17-control-session-changed'])window.addEventListener(event,cancelAll);
