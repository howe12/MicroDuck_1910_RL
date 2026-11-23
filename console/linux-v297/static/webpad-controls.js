const initial=()=>({lx:0,ly:0,rx:0,ry:0,lt:0,rt:0,dx:0,dy:0});
export const padInputReady=s=>s?.service_sample_fresh===true&&s?.bus?.mode==='motion'&&s.bus.phase==='control'&&s.bus.homed===true&&s.bus.policy_enabled===true;
const holdIndependentButton=button=>button==='start'||button==='select';
const holdIndependentTrigger=axis=>axis==='lt'||axis==='rt';
export const gatePadFrame=(frame,ready)=>ready?frame:{axes:{...initial(),lt:frame.axes.lt,rt:frame.axes.rt},buttons:frame.buttons.filter(holdIndependentButton)};
export class PadControls {
  constructor(panel){
    this.panel=panel;this.root=panel.root.querySelector('.official-pad');this.axes=initial();this.buttons=new Set();this.inputs=new Map();this.boundButtons=new Set();
    for(const stick of this.root.querySelectorAll('[data-stick]')){
      stick.addEventListener('pointerdown',e=>{if(e.button||!this.canUse(stick))return;e.preventDefault();stick.setPointerCapture(e.pointerId);this.inputs.set(e.pointerId,{stick});stick.classList.add('held');this.drag(stick,e);});
      stick.addEventListener('pointermove',e=>{if(this.inputs.get(e.pointerId)?.stick===stick)this.drag(stick,e);});
      stick.addEventListener('lostpointercapture',e=>this.release(e.pointerId));stick.addEventListener('contextmenu',e=>e.preventDefault());
    }
    for(const button of this.root.querySelectorAll('[data-button],[data-dpad],[data-trigger]'))this.bindButton(button);
    for(const trigger of this.root.querySelectorAll('[data-axis]')){
      trigger.addEventListener('input',()=>{if(!this.canUse(trigger))return;this.axes[trigger.dataset.axis]=Number(trigger.value);panel.change();});
      for(const event of ['pointerup','pointercancel','blur'])trigger.addEventListener(event,()=>{trigger.value=0;this.axes[trigger.dataset.axis]=0;panel.change(false);});
    }
    window.addEventListener('pointerup',e=>this.release(e.pointerId));window.addEventListener('pointercancel',e=>this.release(e.pointerId));
    const center=this.root.querySelector('[data-pad-center]');center.onclick=()=>{if(!this.canUse(center))return;this.reset();panel.change(false);};
  }
  canUse(control){return !this.panel.disabled&&control.dataset.unavailable!=='true'&&(this.panel.padReady===true||holdIndependentButton(control.dataset.button)||holdIndependentTrigger(control.dataset.trigger||control.dataset.axis));}
  bindButton(button){
    if(this.boundButtons.has(button))return;
    this.boundButtons.add(button);
    button.addEventListener('pointerdown',e=>{if(e.button||button.disabled||!this.canUse(button))return;e.preventDefault();button.setPointerCapture(e.pointerId);this.inputs.set(e.pointerId,{button});this.applyButton(button,true);});
    button.addEventListener('lostpointercapture',e=>this.release(e.pointerId));
    button.addEventListener('keydown',e=>{if(['Space','Enter'].includes(e.code)&&!e.repeat&&!button.disabled&&this.canUse(button)){e.preventDefault();this.applyButton(button,true);}});
    button.addEventListener('keyup',e=>{if(['Space','Enter'].includes(e.code)){e.preventDefault();this.applyButton(button,false);}});
    button.addEventListener('blur',()=>{if(button.classList.contains('held'))this.applyButton(button,false);});
  }
  applyButton(button,pressed){
    if(pressed&&(button.disabled||!this.canUse(button)))return;
    if(pressed&&button===this.panel.startAlias)this.panel.releaseStartOwnerOnIdle=true;
    if(button.dataset.trigger){this.axes[button.dataset.trigger]=pressed?1:0;}
    else if(button.dataset.button){if(pressed)this.buttons.add(button.dataset.button);else this.buttons.delete(button.dataset.button);}
    else {const [axis,value]=button.dataset.dpad.split(':');this.axes[axis]=pressed?Number(value):0;}
    button.classList.toggle('held',pressed);button.setAttribute('aria-pressed',String(pressed));this.panel.change(pressed);
  }
  drag(stick,e){
    if(!this.canUse(stick))return;
    const r=stick.getBoundingClientRect(),x=Math.max(-1,Math.min(1,(e.clientX-r.left-r.width/2)/(r.width/2))),y=Math.max(-1,Math.min(1,(e.clientY-r.top-r.height/2)/(r.height/2)));
    const prefix=stick.dataset.stick;this.axes[prefix+'x']=x;this.axes[prefix+'y']=y;
    stick.querySelector('i').style.transform=`translate(${x*35}px,${y*35}px)`;this.panel.change();
  }
  release(id){
    const input=this.inputs.get(id);if(!input)return;this.inputs.delete(id);
    if(input.button)this.applyButton(input.button,false);
    if(input.stick){const p=input.stick.dataset.stick;this.axes[p+'x']=this.axes[p+'y']=0;input.stick.classList.remove('held');input.stick.querySelector('i').style.transform='translate(0,0)';this.panel.change(false);}
  }
  active(){return this.buttons.size>0||Object.values(this.axes).some(v=>v!==0);}
  frame(){return {axes:{...this.axes},buttons:[...this.buttons]};}
  reset(){
    this.inputs.clear();this.axes=initial();this.buttons.clear();
    for(const stick of this.root.querySelectorAll('[data-stick]')){stick.classList.remove('held');stick.querySelector('i').style.transform='translate(0,0)';}
    for(const b of this.boundButtons){b.classList.remove('held');b.setAttribute('aria-pressed','false');}
    for(const input of this.root.querySelectorAll('[data-axis]'))input.value=0;
  }
  paint(){
    for(const button of new Set([...this.root.querySelectorAll('button'),...this.boundButtons]))button.disabled=!this.canUse(button);
    for(const input of this.root.querySelectorAll('input'))input.disabled=!this.canUse(input);
    for(const stick of this.root.querySelectorAll('[data-stick]'))stick.setAttribute('aria-disabled',String(!this.canUse(stick)));
    this.root.classList.toggle('pad-ready',this.panel.padReady===true);
    this.root.classList.toggle('pad-awaiting-hold',!this.panel.disabled&&this.panel.padReady!==true);
  }
}
