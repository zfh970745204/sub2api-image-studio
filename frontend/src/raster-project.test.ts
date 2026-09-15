// @vitest-environment node
import { describe, expect, it } from "vitest";
import { encodeProject, decodeProject, type ProjectManifest } from "./raster-project";

describe("layer document framing", () => {
  const layers = [{ id:"base", name:"原图", visible:true, locked:false, opacity:100, byteLength:3 }, { id:"paint", name:"填色", visible:false, locked:true, opacity:45, byteLength:4 }];
  const manifest: ProjectManifest = {version:1,width:2,height:3,activeId:"paint",layers};
  const blobs = [new Blob(["abc"]),new Blob(["defg"])];
  it("round-trips exact PNG bytes and layer settings without base64", async () => {
    const loaded = await decodeProject(encodeProject(manifest,blobs),2,3);
    expect(loaded.manifest).toEqual(manifest);
    expect(await Promise.all(loaded.blobs.map((blob)=>blob.text()))).toEqual(["abc","defg"]);
  });
  it("rejects bad dimensions, duplicated ids, missing bytes and invalid headers before decoding images", async () => {
    const good = encodeProject(manifest,blobs);
    await expect(decodeProject(good,3,2)).rejects.toThrow("尺寸");
    await expect(decodeProject(good.slice(0,good.size-1),2,3)).rejects.toThrow("数据");
    await expect(decodeProject(new Blob([good,"extra"]),2,3)).rejects.toThrow("不完整");
    await expect(decodeProject(encodeProject({...manifest,layers:[layers[0],layers[0]]},blobs),2,3)).rejects.toThrow("数据");
    await expect(decodeProject(new Blob([new Uint8Array([255,255,255,255,1])]),2,3)).rejects.toThrow("文件头");
  });
});
