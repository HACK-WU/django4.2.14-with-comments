"""
A class for storing a tree graph. Primarily used for filter constructs in the
ORM.
存储树形图的类。主要用于中的筛选器构造ORM。
"""

import copy

from django.utils.hashable import make_hashable


class Node:
    """
    A single internal node in the tree graph. A Node should be viewed as a
    connection (the root) with the children being either leaf nodes or other
    Node instances.
    树状图中的单个内部节点。节点应被视为与子节点（可以是叶节点或其他Node实例）的连接（根）。
    """

    # Standard connector type. Clients usually won't use this at all and
    # subclasses will usually override the value.
    # 标准连接器类型。客户端通常不会直接使用，子类通常会覆盖该值。
    default = "DEFAULT"

    def __init__(self, children=None, connector=None, negated=False):
        """构造一个新的Node。如果没有提供连接器，则使用默认值。"""
        self.children = children[:] if children else []
        self.connector = connector or self.default
        self.negated = negated

    @classmethod
    def create(cls, children=None, connector=None, negated=False):
        """
        使用Node()而不是__init__()创建一个新的实例，因为一些子类，如django.db.models.query_utils.Q，
        可能会实现一个与Node.__init__()签名冲突的自定义__init__()。
        """
        obj = Node(children, connector or cls.default, negated)
        obj.__class__ = cls
        return obj

    def __str__(self):
        # 根据是否negated，生成节点的字符串表示。
        template = "(NOT (%s: %s))" if self.negated else "(%s: %s)"
        return template % (self.connector, ", ".join(str(c) for c in self.children))

    def __repr__(self):
        # 生成节点的官方字符串表示，通常用于调试。
        return "<%s: %s>" % (self.__class__.__name__, self)

    def __copy__(self):
        # 创建节点的浅复制。
        obj = self.create(connector=self.connector, negated=self.negated)
        obj.children = self.children  # 不使用[:]，因为.create()已经处理了。
        return obj

    copy = __copy__

    def __deepcopy__(self, memodict):
        # 创建节点的深复制。
        obj = self.create(connector=self.connector, negated=self.negated)
        obj.children = copy.deepcopy(self.children, memodict)
        return obj

    def __len__(self):
        """返回此节点拥有的子节点数量。"""
        return len(self.children)

    def __bool__(self):
        """返回此节点是否有子节点。"""
        return bool(self.children)

    def __contains__(self, other):
        """如果'other'是此实例的直接子节点，则返回True。"""
        return other in self.children

    def __eq__(self, other):
        # 检查两个节点是否相等。
        return (
            self.__class__ == other.__class__
            and self.connector == other.connector
            and self.negated == other.negated
            and self.children == other.children
        )

    def __hash__(self):
        # 计算节点的哈希值。
        return hash(
            (
                self.__class__,
                self.connector,
                self.negated,
                *make_hashable(self.children),
            )
        )

    def add(self, data, conn_type):
        """
         Combine this tree and the data represented by data using the
        connector conn_type. The combine is done by squashing the node other
        away if possible.

        This tree (self) will never be pushed to a child node of the
        combined tree, nor will the connector or negated properties change.

        Return a node which can be used in place of data regardless if the
        node other got squashed or not.
        
        使用连接器conn_type组合此树和data表示的数据。如果可能，通过消除other节点来组合。
        此树（self）永远不会被推送到组合树的子节点中，连接器或negated属性也不会改变。
        无论节点other是否被消除，返回一个可以替代data的节点。

        1、如果当前树的连接器与指定的不同，则更新当前树并将其作为子节点添加到新节点中。
        2、若data为Node类型且未被否定，并满足特定条件，则直接合并其子节点。
        3、其他情况下，将data添加为当前树的子节点。
        """
        if self.connector != conn_type:
            obj = self.copy()
            self.connector = conn_type
            self.children = [obj, data]
            return data
        elif (
            isinstance(data, Node)
            and not data.negated
            and (data.connector == conn_type or len(data) == 1)
        ):
            # We can squash the other node's children directly into this node.
            # We are just doing (AB)(CD) == (ABCD) here, with the addition that
            # if the length of the other node is 1 the connector doesn't
            # matter. However, for the len(self) == 1 case we don't want to do
            # the squashing, as it would alter self.connector.
            # 可以直接将other节点的子节点压缩到此节点中。
            self.children.extend(data.children)
            return self
        else:
            # We could use perhaps additional logic here to see if some
            # children could be used for pushdown here.
            self.children.append(data)
            return data

    def negate(self):
        """否定根连接器的含义。"""
        self.negated = not self.negated

