/** One connection attempt; only an explicit user action creates a new gate. */
export class ConnectionRetry {
  constructor(clock=()=>Date.now()){this.clock=clock;this.failures=0;this.until=0;this.since=null;}
  get exhausted(){return this.failures>=1;}
  get ready(){return !this.exhausted;}
  connected(){if(this.since===null)this.since=this.clock();}
  failed(){
    this.since=null;this.failures++;this.until=0;
    return '连接已停止，请点击顶部重连';
  }
}
