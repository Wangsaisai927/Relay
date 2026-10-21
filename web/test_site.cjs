// Unit-test DOM/canvas calls using test doubles; this is not visual browser QA.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('dist/index.html','utf8');
const payload=html.match(/<script id="relay-payload" type="application\/json">([\s\S]*?)<\/script>/)[1];
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const DATA=JSON.parse(payload),nodes=[];
const ctx=new Proxy({measureText:s=>({width:String(s).length*6})},{get:(t,k)=>k in t?t[k]:()=>{},set:(t,k,v)=>(t[k]=v,true)});
class Element{
 constructor(tag='div'){this.tagName=tag.toUpperCase();this.id='';this.children=[];this.dataset={};this.style={};this.textContent='';this.value='0';this.listeners={};this.classList={toggle(){}};nodes.push(this);}
 append(...items){this.children.push(...items);} replaceChildren(...items){this.children=items;}
 addEventListener(n,fn){this.listeners[n]=fn;}setAttribute(n,v){this[n]=v;}
 getContext(){return ctx;}getBoundingClientRect(){return {width:360,height:230};}
 click(){this.listeners.click?.({target:this});}
}
for(const [,id] of html.matchAll(/id="([^"]+)"/g)){const e=new Element(id.startsWith('rx')?'canvas':'div');e.id=id;}
function get(id){return nodes.find(n=>n.id===id);}
get('relay-payload').textContent=payload;get('event1').dataset.event='Event1';get('event2').dataset.event='Event2';
const legend=Array.from({length:3},()=>new Element());const notes={};
const document={getElementById:get,createElement:t=>new Element(t),activeElement:{tagName:'BODY'},addEventListener(){},
 querySelector:s=>notes[s]??(notes[s]=new Element()),
 querySelectorAll:s=>s==='[data-event]'?[get('event1'),get('event2')]:s==='[data-loop]'?nodes.filter(n=>n.dataset.loop):s==='.rx-legend span b'?legend:[]};
const window={devicePixelRatio:1,matchMedia:()=>({matches:true}),addEventListener(){}};
const env=vm.createContext({document,window,requestAnimationFrame(){},setTimeout(){},clearTimeout(){},console});
new vm.Script(script).runInContext(env);
assert.equal(get('sixGrid').children.length,6);
for(const name of ['Event1','Event2']){
 vm.runInContext(`chooseEvent('${name}',false);S.time=1.04;render();`,env);
 for(const loop of ['AG','BG','CG','AB','BC','CA']){
  vm.runInContext(`inspectLoop='${loop}';render();`,env);
  assert(get('zLabel').textContent.includes(loop));
  const e=DATA[name];assert.equal(e.loopData[loop].z[0].length,7200);
 }
 vm.runInContext(`S.rx='full';setupRX();render();S.time=1.25;render();`,env);
}
assert.equal(DATA.Event1.tripLoop,'AG');assert.equal(DATA.Event2.tripTime,null);
assert.deepEqual(DATA.Event2.raw,DATA.Event2.after);
console.log('Six-loop payload, startup, event switching, inspectors, resize geometry and render calls passed (test doubles; not pixel QA).');
