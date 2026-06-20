import { useCallback, useEffect } from 'react';
import {
  Background,
  Controls,
  ReactFlow,
  useEdgesState,
  useNodesState,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import TraceNode from './TraceNode.jsx';
import MessageNode from './MessageNode.jsx';

const nodeTypes = { trace: TraceNode, message: MessageNode };

/**
 * Presentational canvas. Graph building + selection live in the parent.
 *
 * @param {{
 *   nodes: Array<Object>,
 *   edges: Array<Object>,
 *   selectedId: string|null,
 *   onSelect: (id: string) => void,
 * }} props
 */
export default function TraceGraph({ nodes: inNodes, edges: inEdges, selectedId, onSelect }) {
  const [nodes, setNodes, onNodesChange] = useNodesState(inNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(inEdges);

  // Re-sync when events arrive; flag the selected node so it can be styled.
  useEffect(() => {
    setNodes(inNodes.map((n) => ({ ...n, selected: n.id === selectedId })));
  }, [inNodes, selectedId, setNodes]);
  useEffect(() => setEdges(inEdges), [inEdges, setEdges]);

  const handleNodeClick = useCallback(
    (_event, node) => onSelect(node.id),
    [onSelect],
  );

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onNodeClick={handleNodeClick}
      nodeTypes={nodeTypes}
      fitView
      fitViewOptions={{ padding: 0.3, maxZoom: 1 }}
      proOptions={{ hideAttribution: true }}
    >
      <Background gap={24} size={1} color="#23262f" />
      <Controls showInteractive={false} />
    </ReactFlow>
  );
}
