// Shared by the existing AI monitor and the HAT speaker test; input is measured dBFS.
export function audioLevelBars(dbfs,count,fresh=true){
  if(!fresh||!Number.isFinite(dbfs)||!Number.isSafeInteger(count)||count<1)return 0;
  return Math.ceil(Math.max(0,Math.min(1,(dbfs+60)/54))*count);
}
