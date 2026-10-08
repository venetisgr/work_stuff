import fs from 'fs';
const b=fs.readFileSync(process.argv[2]);
console.log('magic',b.toString('utf8',0,4),'ver',b.readUInt32LE(4),'len',b.readUInt32LE(8));
const jl=b.readUInt32LE(12);
const j=JSON.parse(b.toString('utf8',20,20+jl));
console.log('scenes',j.scenes?.length,'nodes',j.nodes.length,'meshes',j.meshes?.length,'skins',j.skins?.length,'materials',j.materials?.length,'images',j.images?.length, 'textures', j.textures?.length);
console.log('extensions',j.extensionsUsed, j.extensionsRequired);
console.log('anims',j.animations?.length);
const acc=j.accessors;
for(const a of j.animations||[]){
 let t=0;const chans={};
 for(const s of a.samplers){const m=acc[s.input].max?.[0];if(m>t)t=m}
 for(const c of a.channels){chans[c.target.path]=(chans[c.target.path]||0)+1}
 console.log(JSON.stringify(a.name),'dur',t.toFixed(2),'ch',a.channels.length,JSON.stringify(chans));
}
const sk=j.skins?.[0];
console.log('joints',sk?.joints.length,'root joint',j.nodes[sk?.joints[0]]?.name);
console.log('first nodes',j.nodes.slice(0,8).map(n=>n.name+(n.children?`[${n.children.length}]`:'')));
console.log('mesh nodes',j.nodes.filter(n=>n.mesh!==undefined).map(n=>n.name+(n.scale?JSON.stringify(n.scale):'')));
// bbox from POSITION accessors
let mn=[1e9,1e9,1e9],mx=[-1e9,-1e9,-1e9];
for(const m of j.meshes)for(const p of m.primitives){const a=acc[p.attributes.POSITION];for(let i=0;i<3;i++){mn[i]=Math.min(mn[i],a.min[i]);mx[i]=Math.max(mx[i],a.max[i])}}
console.log('bbox',mn,mx);
const rootNodes=j.scenes[j.scene||0].nodes;console.log('scene roots',rootNodes.map(i=>j.nodes[i].name+JSON.stringify({s:j.nodes[i].scale,t:j.nodes[i].translation,r:j.nodes[i].rotation})));
