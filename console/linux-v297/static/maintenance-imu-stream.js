/** Read one authenticated maintenance SSE response; never reconnect or command hardware. */
export async function consumeMaintenanceEvents(response, {signal, onFrame, eventType='imu', label='IMU'}) {
  if (!response.body?.getReader) throw Error(`浏览器无法读取 ${label} 实时流`);
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = '', data = [], event = '', eventSize = 0;
  const dispatch = () => {
    if (data.length && (!event || event === 'message' || event === eventType)) onFrame(JSON.parse(data.join('\n')));
    data = []; event = ''; eventSize = 0;
  };
  const line = value => {
    eventSize += value.length + 1;
    if (eventSize > 65536) throw Error(`${label} 实时流消息过大`);
    if (!value) {dispatch(); return;}
    if (value.startsWith(':')) return;
    const colon = value.indexOf(':'), key = colon < 0 ? value : value.slice(0, colon);
    let content = colon < 0 ? '' : value.slice(colon + 1); if (content.startsWith(' ')) content = content.slice(1);
    if (key === 'data') data.push(content); else if (key === 'event') event = content;
  };
  const cancel = () => reader.cancel().catch(() => {});
  signal?.addEventListener('abort', cancel, {once:true});
  try {
    while (!signal?.aborted) {
      const {done, value} = await reader.read();
      if (signal?.aborted) break;
      buffer += decoder.decode(value, {stream:!done});
      let end;
      while ((end = buffer.indexOf('\n')) >= 0) {line(buffer.slice(0,end).replace(/\r$/, '')); buffer = buffer.slice(end+1);}
      if (eventSize + buffer.length > 65536) throw Error(`${label} 实时流消息过大`);
      if (done) {if (buffer) line(buffer.replace(/\r$/, '')); dispatch(); break;}
    }
  } finally {signal?.removeEventListener('abort', cancel); await reader.cancel().catch(() => {}); reader.releaseLock();}
}

export function consumeImuEvents(response, options) {
  return consumeMaintenanceEvents(response, {...options,eventType:'imu',label:'IMU'});
}
